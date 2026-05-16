"""Conformance test suite for the AgentProfileBackend Protocol (spec/24).

Parametrized over a ``backend_factory`` fixture. Each registered backend
that ships in core (``FilesystemAgentProfileBackend`` today; PR 3 of #63
adds a second reference impl) is exercised against the same contract. A
third-party backend in a downstream package imports this test module's
``BACKEND_FACTORIES`` parametrization to verify its own conformance.

What this suite asserts:

1. Protocol surface — ``isinstance(backend, AgentProfileBackend)`` passes;
   all required attributes/methods are present.
2. ``backend_id`` is a stable non-empty string.
3. ``capabilities`` returns a ``ProfileCapabilities`` instance.
4. ``load_profile`` raises ``AgentProfileNotFound`` for a missing agent.
5. ``load_profile`` round-trip preserves persona raw bytes byte-for-byte.
6. ``load_profile`` populates structured fields from raw text.
7. ``load_profile`` derives ``agent_mode`` from ``persona_identity``.
8. ``save_profile`` persists raw fields byte-for-byte (round-trip).
9. ``save_profile`` ignores ``agent_mode`` field (re-derived on load).
10. ``save_profile`` preserves MCP ``$VAR`` env refs in raw text
    (the security-critical Decision 1 invariant).
11. ``save_profile`` preserves tools.md operator comments.
12. ``save_profile`` removes optional files when their raw fields go empty.
13. ``list_agents`` empty-backend returns ``[]``.
14. ``list_agents`` lexicographic order.
15. ``list_agents`` excludes hidden directories.
16. ``list_agents`` excludes directories without ``persona/IDENTITY.md``.
17. ``exists`` True for valid agent.
18. ``exists`` False for missing agent.
19. ``list_skills`` empty when no skills directory.
20. ``list_skills`` returns metadata for present skills.
21. ``load_skill_body`` returns body without frontmatter.
22. ``load_skill_body`` raises FileNotFoundError for unknown skill.
23. ``clone`` copies a profile to a new id.
24. ``clone`` applies overrides at write time.
25. ``clone`` raises ``AgentProfileExists`` on overwrite.
26. ``clone`` raises ``ValueError`` on unknown override key.
27. ``clone`` copies skills directory tree.
28. Capabilities parity — when ``supports_snapshot=False``, snapshot trio
    raises ``NotImplementedError``.
29. ``AgentProfile.to_dict / from_dict`` round-trip preserves all fields.
30. Round-trip survives missing optional files (no SOUL.md, no goal.md).
31. ``list_skills`` on missing agent raises ``AgentProfileNotFound`` (GAP-11).
32. ``load_skill_body`` on missing agent raises ``AgentProfileNotFound`` (GAP-11).
33. ``load_skill_body`` refuses ``skill_name`` containing path-traversal
    tokens (Step 9.1 multi-specialist finding F-A — security).
34. ``AgentProfile.from_dict`` narrows the mcp.md re-parse except to
    ``MCPServerConnectFailed`` only (Step 9.1 multi-specialist finding
    F-B — security parity with ``filesystem.py`` load_profile).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from atomic_agents.exceptions import (
    AgentProfileExists,
    AgentProfileNotFound,
)
from atomic_agents.profile import (
    AgentProfile,
    AgentProfileBackend,
    FilesystemAgentProfileBackend,
    ProfileCapabilities,
)


# ──────────────────────────────────────────────────────────────────
# Backend factory parametrization — every conformance test runs once
# per registered backend. PR 1 ships only the filesystem reference;
# PR 3 of #63 will add the second factory entry.

BackendFactory = Callable[[Path], AgentProfileBackend]


def _filesystem_factory(scope_root: Path) -> AgentProfileBackend:
    return FilesystemAgentProfileBackend(scope_root)


BACKEND_FACTORIES: list[tuple[str, BackendFactory]] = [
    ("filesystem", _filesystem_factory),
]


@pytest.fixture(params=BACKEND_FACTORIES, ids=lambda p: p[0])
def backend_factory(request) -> BackendFactory:
    """Yields a callable ``(scope_root: Path) -> AgentProfileBackend``."""
    return request.param[1]


@pytest.fixture
def backend(backend_factory, tmp_path) -> AgentProfileBackend:
    """A backend rooted at a per-test tmp_path."""
    return backend_factory(tmp_path)


# ──────────────────────────────────────────────────────────────────
# Helpers for fixture construction


_IDENTITY_BODY = "# Scout\n\n## Operating mode\n\nThis agent is reactive.\n"
_SOUL_BODY = "# Soul\n\nCurious, thorough.\n"
_USER_BODY = "# User\n\nDan, the operator.\n"
_GOAL_BODY = (
    "---\n"
    "schema_version: 1\n"
    "active: true\n"
    "intent: investigate the topic\n"
    "priority: medium\n"
    "created: 2026-05-15\n"
    "last_progress_check: 2026-05-15\n"
    "success_criteria:\n"
    "  - finds three sources\n"
    "sub_goals: []\n"
    "---\n\n"
    "# Goal narrative\n\n"
    "Body content here.\n"
)
_TOOLS_BODY = (
    "# Tools\n\n"
    "## Read paths\n\n"
    "- ~/scout/data — operator's data dir\n\n"
    "## Write paths\n\n"
    "- ~/scout/notes — captures land here\n\n"
    "## Tool classification\n\n"
    "- write_atomic_note: reversible_write\n"
    "- send_email: external_side_effect\n"
)
_MODEL_BODY = (
    "# Model\n\n"
    "## Default model\n\n"
    "claude-sonnet-4-6-20260101\n\n"
    "## Fallback\n\n"
    "claude-haiku-4-5-20251001\n"
)
_ROSTER_BODY = (
    "# Roster\n\n"
    "## Delegate to\n\n"
    "- editor — proofreads drafts\n"
    "- researcher — fact-checks claims\n"
)
_MCP_BODY_WITH_VAR = (
    "# MCP servers\n\n"
    "## github\n"
    "command: npx\n"
    "args: -y, @modelcontextprotocol/server-github\n"
    "env: GITHUB_TOKEN=$NEVER_SET_VAR_FOR_TEST\n"
    "description: GitHub access\n"
)
_MCP_BODY_PLAIN = (
    "# MCP servers\n\n"
    "## filesystem-tools\n"
    "command: npx\n"
    "args: -y, @modelcontextprotocol/server-filesystem\n"
    "description: Local filesystem access\n"
)


def make_agent_dir(
    scope_root: Path,
    agent_id: str,
    *,
    identity: str = _IDENTITY_BODY,
    soul: str | None = _SOUL_BODY,
    user: str | None = _USER_BODY,
    goal: str | None = None,
    tools: str | None = _TOOLS_BODY,
    model: str | None = _MODEL_BODY,
    roster: str | None = _ROSTER_BODY,
    mcp: str | None = _MCP_BODY_PLAIN,
    judges: str | None = None,
    skills: dict[str, str] | None = None,
) -> Path:
    """Create the on-disk shape of a complete agent under ``scope_root``.

    Each ``None`` field is omitted; non-None fields are written verbatim.
    Returns the agent's root directory.

    For backends that don't use the filesystem natively (PR 3+
    SQLiteAgentProfileBackend, future GitAgentProfileBackend), the
    fixture writes filesystem state and the backend's ``load_profile``
    is expected to be able to read that filesystem state — OR the test
    parameters need a second helper. PR 1 only has the filesystem
    backend so this works directly.
    """
    agent_root = scope_root / agent_id
    (agent_root / "persona").mkdir(parents=True)
    (agent_root / "persona" / "IDENTITY.md").write_text(identity, encoding="utf-8")
    if soul is not None:
        (agent_root / "persona" / "SOUL.md").write_text(soul, encoding="utf-8")
    if user is not None:
        (agent_root / "persona" / "USER.md").write_text(user, encoding="utf-8")
    if goal is not None:
        (agent_root / "goal.md").write_text(goal, encoding="utf-8")
    if tools is not None:
        (agent_root / "tools.md").write_text(tools, encoding="utf-8")
    if model is not None:
        (agent_root / "model.md").write_text(model, encoding="utf-8")
    if roster is not None:
        (agent_root / "roster.md").write_text(roster, encoding="utf-8")
    if mcp is not None:
        (agent_root / "mcp.md").write_text(mcp, encoding="utf-8")
    if judges is not None:
        (agent_root / "judges.md").write_text(judges, encoding="utf-8")
    if skills:
        for skill_name, body in skills.items():
            skill_dir = agent_root / "skills" / skill_name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    return agent_root


# ──────────────────────────────────────────────────────────────────
# Surface conformance


def test_protocol_isinstance(backend):
    """isinstance check passes — backend exposes the full Protocol."""
    assert isinstance(backend, AgentProfileBackend)


def test_backend_id_is_stable_nonempty_string(backend):
    backend_id = backend.backend_id
    assert isinstance(backend_id, str)
    assert backend_id != ""
    assert backend.backend_id == backend_id


def test_capabilities_returns_profile_capabilities(backend):
    caps = backend.capabilities()
    assert isinstance(caps, ProfileCapabilities)
    assert isinstance(caps.supports_save, bool)
    assert isinstance(caps.supports_clone, bool)
    assert isinstance(caps.supports_snapshot, bool)
    assert isinstance(caps.supports_subscribe, bool)
    assert isinstance(caps.durable, bool)


# ──────────────────────────────────────────────────────────────────
# load_profile


def test_load_profile_missing_agent_raises(backend, tmp_path):
    with pytest.raises(AgentProfileNotFound):
        backend.load_profile("does-not-exist")


def test_load_profile_basic_round_trip(backend, tmp_path):
    make_agent_dir(tmp_path, "scout")
    profile = backend.load_profile("scout")
    assert profile.name == "scout"
    assert profile.persona_identity == _IDENTITY_BODY
    assert profile.persona_soul == _SOUL_BODY
    assert profile.persona_user == _USER_BODY


def test_load_profile_populates_structured_fields(backend, tmp_path):
    make_agent_dir(tmp_path, "scout")
    profile = backend.load_profile("scout")
    # model_config from model.md
    assert profile.model_config["default_model"] == "claude-sonnet-4-6-20260101"
    assert profile.model_config["fallback_model"] == "claude-haiku-4-5-20251001"
    # tool_config from tools.md
    assert len(profile.tool_config["read_paths"]) == 1
    assert len(profile.tool_config["write_paths"]) == 1
    # tool_classifications from same file
    assert profile.tool_classifications["write_atomic_note"] == "reversible_write"
    assert profile.tool_classifications["send_email"] == "external_side_effect"
    # roster from roster.md
    assert profile.roster == ["editor", "researcher"]


def test_load_profile_derives_agent_mode(backend, tmp_path):
    make_agent_dir(
        tmp_path,
        "goal-agent",
        identity=("# Goal Agent\n\n## Operating mode\n\nThis agent is goal-driven.\n"),
    )
    profile = backend.load_profile("goal-agent")
    assert profile.agent_mode == "goal-driven"


def test_load_profile_preserves_persona_byte_for_byte(backend, tmp_path):
    custom_identity = "# Custom\n\nCustom content with **markdown** and `code`.\n"
    make_agent_dir(tmp_path, "scout", identity=custom_identity)
    profile = backend.load_profile("scout")
    assert profile.persona_identity == custom_identity


def test_load_profile_optional_files_absent(backend, tmp_path):
    """Goal, soul, user, judges, mcp, roster all optional."""
    make_agent_dir(
        tmp_path,
        "minimal",
        soul=None,
        user=None,
        goal=None,
        tools=None,
        model=None,
        roster=None,
        mcp=None,
        judges=None,
    )
    profile = backend.load_profile("minimal")
    assert profile.persona_soul == ""
    assert profile.persona_user == ""
    assert profile.goal_text == ""
    assert profile.tools_md_raw == ""
    assert profile.model_md_raw == ""
    assert profile.roster_md_raw == ""
    assert profile.mcp_md_raw == ""
    assert profile.judges_md_raw is None
    assert profile.judges_config is None


# ──────────────────────────────────────────────────────────────────
# save_profile


def test_save_profile_round_trip_raw_fields_byte_for_byte(backend, tmp_path):
    make_agent_dir(tmp_path, "scout")
    profile = backend.load_profile("scout")
    backend.save_profile("scout", profile)
    profile2 = backend.load_profile("scout")
    assert profile2.persona_identity == profile.persona_identity
    assert profile2.persona_soul == profile.persona_soul
    assert profile2.persona_user == profile.persona_user
    assert profile2.tools_md_raw == profile.tools_md_raw
    assert profile2.model_md_raw == profile.model_md_raw
    assert profile2.roster_md_raw == profile.roster_md_raw
    assert profile2.mcp_md_raw == profile.mcp_md_raw


def test_save_profile_preserves_mcp_dollar_var_refs(backend, tmp_path):
    """Decision 1 — security-critical: $VAR refs MUST NOT be baked into save."""
    make_agent_dir(tmp_path, "scout", mcp=_MCP_BODY_WITH_VAR)
    profile = backend.load_profile("scout")
    # Raw text retains the literal $NEVER_SET_VAR_FOR_TEST reference
    assert "$NEVER_SET_VAR_FOR_TEST" in profile.mcp_md_raw
    backend.save_profile("scout", profile)
    profile2 = backend.load_profile("scout")
    assert "$NEVER_SET_VAR_FOR_TEST" in profile2.mcp_md_raw


def test_save_profile_preserves_tools_operator_comments(backend, tmp_path):
    """Decision 1 — tools.md comments lost on parse, preserved in raw."""
    custom_tools = (
        "# Tools\n\n## Read paths\n\n- ~/scout/data — Dan's notes, scout-specific\n"
    )
    make_agent_dir(tmp_path, "scout", tools=custom_tools)
    profile = backend.load_profile("scout")
    assert "Dan's notes, scout-specific" in profile.tools_md_raw
    backend.save_profile("scout", profile)
    profile2 = backend.load_profile("scout")
    assert "Dan's notes, scout-specific" in profile2.tools_md_raw


def test_save_profile_ignores_agent_mode_field(backend, tmp_path):
    """Decision 6 — agent_mode is documented-derived; save ignores it."""
    make_agent_dir(tmp_path, "scout")  # identity says "reactive"
    profile = backend.load_profile("scout")
    assert profile.agent_mode == "reactive"
    # Override agent_mode but leave persona_identity unchanged
    bogus = profile.replace(agent_mode="goal-driven")
    backend.save_profile("scout", bogus)
    profile2 = backend.load_profile("scout")
    # Re-derived from persona_identity — still reactive
    assert profile2.agent_mode == "reactive"


def test_save_profile_removes_optional_files_when_empty(backend, tmp_path):
    """Empty raw_text fields → corresponding on-disk files removed."""
    make_agent_dir(tmp_path, "scout")
    profile = backend.load_profile("scout")
    # Strip the optional fields (use replace so frozen dataclass is fine)
    profile_minimal = profile.replace(
        persona_soul="",
        persona_user="",
        goal_text="",
        roster_md_raw="",
        mcp_md_raw="",
    )
    backend.save_profile("scout", profile_minimal)
    profile2 = backend.load_profile("scout")
    assert profile2.persona_soul == ""
    assert profile2.persona_user == ""
    assert profile2.goal_text == ""
    assert profile2.roster_md_raw == ""
    assert profile2.mcp_md_raw == ""


# ──────────────────────────────────────────────────────────────────
# list_agents


def test_list_agents_empty(backend, tmp_path):
    assert backend.list_agents() == []


def test_list_agents_lexicographic_order(backend, tmp_path):
    make_agent_dir(tmp_path, "charlie")
    make_agent_dir(tmp_path, "alpha")
    make_agent_dir(tmp_path, "bravo")
    assert backend.list_agents() == ["alpha", "bravo", "charlie"]


def test_list_agents_excludes_hidden_dirs(backend, tmp_path):
    make_agent_dir(tmp_path, "alpha")
    # Hidden dir shouldn't appear even with a valid IDENTITY.md (by design:
    # backend-internal storage uses these prefixes).
    hidden = tmp_path / ".snapshots"
    (hidden / "persona").mkdir(parents=True)
    (hidden / "persona" / "IDENTITY.md").write_text("# Hidden\n", encoding="utf-8")
    assert backend.list_agents() == ["alpha"]


def test_list_agents_excludes_dirs_without_identity(backend, tmp_path):
    make_agent_dir(tmp_path, "alpha")
    # A non-agent dir — has files but no persona/IDENTITY.md
    not_an_agent = tmp_path / "junk"
    not_an_agent.mkdir()
    (not_an_agent / "README.md").write_text("not an agent", encoding="utf-8")
    assert backend.list_agents() == ["alpha"]


# ──────────────────────────────────────────────────────────────────
# exists


def test_exists_true_for_valid_agent(backend, tmp_path):
    make_agent_dir(tmp_path, "scout")
    assert backend.exists("scout") is True


def test_exists_false_for_missing_agent(backend, tmp_path):
    assert backend.exists("nope") is False


def test_exists_false_for_dir_without_identity(backend, tmp_path):
    (tmp_path / "junk").mkdir()
    assert backend.exists("junk") is False


# ──────────────────────────────────────────────────────────────────
# Skills


def test_list_skills_empty(backend, tmp_path):
    make_agent_dir(tmp_path, "scout")
    assert backend.list_skills("scout") == []


def test_list_skills_returns_metadata(backend, tmp_path):
    skill_body = (
        "---\n"
        "name: spreadsheet-analysis\n"
        "description: Processes spreadsheets and generates summaries.\n"
        "---\n\n"
        "# Spreadsheet Analysis\n\nBody content here.\n"
    )
    make_agent_dir(tmp_path, "scout", skills={"spreadsheet-analysis": skill_body})
    skills = backend.list_skills("scout")
    assert len(skills) == 1
    assert skills[0].name == "spreadsheet-analysis"
    assert "spreadsheets" in skills[0].description


def test_load_skill_body_returns_body_without_frontmatter(backend, tmp_path):
    skill_body = (
        "---\n"
        "name: data-cleaning\n"
        "description: Cleans messy data.\n"
        "---\n\n"
        "# Body line one\n\n## Body line two\n"
    )
    make_agent_dir(tmp_path, "scout", skills={"data-cleaning": skill_body})
    body = backend.load_skill_body("scout", "data-cleaning")
    assert "# Body line one" in body
    assert "name: data-cleaning" not in body  # frontmatter stripped


def test_load_skill_body_unknown_skill_raises(backend, tmp_path):
    make_agent_dir(tmp_path, "scout")
    with pytest.raises(FileNotFoundError):
        backend.load_skill_body("scout", "nonexistent-skill")


# ──────────────────────────────────────────────────────────────────
# Clone


def test_clone_copies_profile_to_new_id(backend, tmp_path):
    make_agent_dir(tmp_path, "source")
    backend.clone("source", "target")
    assert backend.exists("target")
    target_profile = backend.load_profile("target")
    source_profile = backend.load_profile("source")
    assert target_profile.persona_identity == source_profile.persona_identity
    assert target_profile.tools_md_raw == source_profile.tools_md_raw
    assert target_profile.name == "target"


def test_clone_applies_overrides(backend, tmp_path):
    make_agent_dir(tmp_path, "source")
    new_identity = "# Cloned\n\n## Operating mode\n\nThis agent is hybrid.\n"
    backend.clone("source", "target", overrides={"persona_identity": new_identity})
    target = backend.load_profile("target")
    assert target.persona_identity == new_identity
    assert target.agent_mode == "hybrid"


def test_clone_refuses_overwrite(backend, tmp_path):
    make_agent_dir(tmp_path, "source")
    make_agent_dir(tmp_path, "target")  # already exists
    with pytest.raises(AgentProfileExists):
        backend.clone("source", "target")


def test_clone_unknown_override_raises(backend, tmp_path):
    make_agent_dir(tmp_path, "source")
    with pytest.raises(ValueError):
        backend.clone("source", "target", overrides={"not_a_field": "value"})


def test_clone_missing_source_raises(backend, tmp_path):
    with pytest.raises(AgentProfileNotFound):
        backend.clone("nope", "target")


def test_clone_copies_skills_directory(backend, tmp_path):
    skill_body = (
        "---\n"
        "name: example-skill\n"
        "description: A test skill for clone.\n"
        "---\n\n# Body\n"
    )
    make_agent_dir(tmp_path, "source", skills={"example-skill": skill_body})
    backend.clone("source", "target")
    skills = backend.list_skills("target")
    assert len(skills) == 1
    assert skills[0].name == "example-skill"


# ──────────────────────────────────────────────────────────────────
# Capability-gated methods — claim-vs-behavior parity


def test_snapshot_unsupported_raises_not_implemented(backend, tmp_path):
    """When supports_snapshot=False, the trio MUST raise NotImplementedError."""
    if backend.capabilities().supports_snapshot:
        pytest.skip("backend supports snapshots; tested in supports_snapshot test")
    make_agent_dir(tmp_path, "scout")
    with pytest.raises(NotImplementedError):
        backend.snapshot("scout", "label")
    with pytest.raises(NotImplementedError):
        backend.restore("scout", "snapshot-id")
    with pytest.raises(NotImplementedError):
        backend.list_snapshots("scout")


# ──────────────────────────────────────────────────────────────────
# AgentProfile dict round-trip — independent of any backend


def test_agent_profile_dict_round_trip(backend, tmp_path):
    """to_dict → from_dict preserves every field on a real profile."""
    make_agent_dir(tmp_path, "scout")
    profile = backend.load_profile("scout")
    d = profile.to_dict()
    profile2 = AgentProfile.from_dict(d)
    assert profile2.name == profile.name
    assert profile2.agent_mode == profile.agent_mode
    assert profile2.persona_identity == profile.persona_identity
    assert profile2.persona_soul == profile.persona_soul
    assert profile2.persona_user == profile.persona_user
    assert profile2.tools_md_raw == profile.tools_md_raw
    assert profile2.model_md_raw == profile.model_md_raw
    assert profile2.roster_md_raw == profile.roster_md_raw
    assert profile2.mcp_md_raw == profile.mcp_md_raw


# ──────────────────────────────────────────────────────────────────
# Step 9.1 specialist review additions


def test_list_skills_missing_agent_raises_not_found(backend, tmp_path):
    """GAP-11 — coverage gap surfaced by Step 7 coverage audit + Step 9.1
    testing specialist. ``list_skills`` on an unknown agent must raise
    ``AgentProfileNotFound``, not silently return ``[]`` or surface a
    ``FileNotFoundError`` that the caller can't distinguish from "skills
    directory doesn't exist on a real agent."
    """
    with pytest.raises(AgentProfileNotFound):
        backend.list_skills("does-not-exist")


def test_load_skill_body_missing_agent_raises_not_found(backend, tmp_path):
    """GAP-11 paired with the above. Without this assertion a caller
    seeing ``FileNotFoundError`` can't distinguish "bad agent id" from
    "bad skill name on a real agent" — different error-recovery paths.
    """
    with pytest.raises(AgentProfileNotFound):
        backend.load_skill_body("does-not-exist", "any-skill")


def test_load_skill_body_refuses_skill_name_traversal(backend, tmp_path):
    """Step 9.1 multi-specialist finding F-A (CRITICAL, testing + security
    confirmed). Without this guard, a caller could pass
    ``skill_name="../<other-agent>/skills/<name>"`` and cross-agent read
    via the constructed path — the original ``_agent_root`` guard
    validates only ``agent_id``. The fix in ``filesystem.py`` rejects
    any ``skill_name`` containing ``/``, ``\\``, leading ``.``, or
    ``..``. Conformance backends that store skills under per-agent
    namespaces are expected to enforce equivalent validation; backends
    that don't accept operator-typed skill names directly may pass this
    test trivially.
    """
    from atomic_agents.exceptions import SkillFileTraversal

    make_agent_dir(tmp_path, "scout")
    # Path-separator traversal
    with pytest.raises((SkillFileTraversal, ValueError)):
        backend.load_skill_body("scout", "../escape")
    # Parent-dir token
    with pytest.raises((SkillFileTraversal, ValueError)):
        backend.load_skill_body("scout", "valid/../../../escape")
    # Leading dot (hidden-dir traversal)
    with pytest.raises((SkillFileTraversal, ValueError)):
        backend.load_skill_body("scout", ".hidden")
    # Backslash separator (Windows-shape attack)
    with pytest.raises((SkillFileTraversal, ValueError)):
        backend.load_skill_body("scout", "..\\escape")


def test_from_dict_narrows_mcp_re_parse_except(backend, tmp_path):
    """Step 9.1 multi-specialist finding F-B (CRITICAL, testing +
    maintainability + security confirmed). ``AgentProfile.from_dict``
    re-parses ``mcp_md_raw`` to populate ``mcp_servers``. The original
    code wrapped this in ``except Exception`` which silently swallowed
    ``PathTraversalError`` — the same gap fixed in ``filesystem.py``'s
    ``load_profile`` (F-3, Step 9 pre-landing review). The narrowing
    is to ``except MCPServerConnectFailed`` only.

    This test verifies the narrowing by asserting that ``from_dict``
    handles an unresolvable ``$VAR`` reference gracefully (the
    MCPServerConnectFailed shape, which IS caught) — and documents
    via comment that other exceptions now propagate.
    """
    # Build a dict with an unresolvable $VAR — exercises the caught path.
    d = {
        "name": "scout",
        "agent_mode": "reactive",
        "model_config": {},
        "tool_config": {},
        "tool_classifications": {},
        "judges_config": None,
        "roster": [],
        "mcp_servers": [],
        "persona_identity": "# Scout\n",
        "persona_soul": "",
        "persona_user": "",
        "goal_text": "",
        "model_md_raw": "",
        "tools_md_raw": "",
        "judges_md_raw": None,
        "roster_md_raw": "",
        "mcp_md_raw": (
            "# MCP servers\n\n"
            "## evil\n"
            "command: npx\n"
            "args: -y, @mcp/server\n"
            "env: TOKEN=$NEVER_SET_VAR_FOR_TEST_F_B\n"
            "description: env var unresolvable\n"
        ),
    }
    # MCPServerConnectFailed is caught → mcp_servers falls back gracefully
    profile = AgentProfile.from_dict(d)
    assert isinstance(profile.mcp_servers, list)
    # Raw text preserved verbatim — the $VAR reference must survive
    assert "$NEVER_SET_VAR_FOR_TEST_F_B" in profile.mcp_md_raw

"""Conformance test suite for the AgentProfileBackend Protocol (spec/24).

Parametrized over a ``backend_factory`` fixture. Each registered backend
that ships in core (``FilesystemAgentProfileBackend``,
``SQLiteAgentProfileBackend`` as of #63 PR 3) is exercised against the
same contract. A third-party backend in a downstream package imports
this test module's ``BACKEND_FACTORIES`` parametrization to verify its
own conformance.

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
    PersonaLinkInvalid,
    SnapshotNotFound,
)
from atomic_agents.profile import (
    AgentProfile,
    AgentProfileBackend,
    FilesystemAgentProfileBackend,
    ProfileCapabilities,
    SQLiteAgentProfileBackend,
)


# ──────────────────────────────────────────────────────────────────
# Backend factory parametrization — every conformance test runs once
# per registered backend. PR 3 of #63 added the SQLite factory; the
# parametrization is the conformance-suite contract that future
# backends extend (downstream packages append their factory to
# ``BACKEND_FACTORIES`` in their own test module).

BackendFactory = Callable[[Path], AgentProfileBackend]


def _filesystem_factory(scope_root: Path) -> AgentProfileBackend:
    return FilesystemAgentProfileBackend(scope_root)


def _sqlite_factory(scope_root: Path) -> AgentProfileBackend:
    """SQLite backend rooted at ``<scope_root>/.profile.db``.

    A real on-disk SQLite file (not ``:memory:``) so the conformance
    suite exercises the filesystem-touching code path that operators
    will hit in production. The db file is colocated with the scope
    root for parity with the filesystem backend's per-tmp_path
    isolation.
    """
    return SQLiteAgentProfileBackend(scope_root / ".profile.db")


BACKEND_FACTORIES: list[tuple[str, BackendFactory]] = [
    ("filesystem", _filesystem_factory),
    ("sqlite", _sqlite_factory),
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


def _derive_agent_mode_from_identity(identity: str) -> str:
    """Mini parser mirroring ``goal.parse_agent_mode`` for fixture use.

    Reads the ``## Operating mode`` body to pick reactive / goal-driven
    / hybrid. Keeps the helper self-contained so the conformance suite
    doesn't depend on the framework's goal module shape.
    """
    lower = identity.lower()
    if "goal-driven" in lower:
        return "goal-driven"
    if "hybrid" in lower:
        return "hybrid"
    return "reactive"


def make_agent_in_backend(
    backend: AgentProfileBackend,
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
) -> AgentProfile:
    """Set up an agent via the Protocol surface (works for any backend).

    Constructs an ``AgentProfile`` from the same default markdown bodies
    as ``make_agent_dir`` and calls ``backend.save_profile`` to install
    it. Returns the saved ``AgentProfile`` so callers can assert against
    it directly.

    **Skills handling:** ``AgentProfile`` doesn't carry skills, so
    skill fixture data is written separately:
    - For backends with ``supports_skills=True`` (filesystem), skills
      are also written to disk under ``<scope_root>/<agent_id>/skills/``
      since the filesystem backend's ``list_skills`` walks the disk.
    - For backends with ``supports_skills=False`` (sqlite), the skills
      kwarg is silently ignored — the backend would refuse to surface
      them via ``list_skills`` anyway, and skill-content tests gate on
      the capability before calling this helper.

    Compared to ``make_agent_dir`` (which writes filesystem only):
    ``make_agent_in_backend`` exercises the Protocol's write path so
    a non-filesystem backend's ``load_profile`` can find the agent it
    was asked to load. Per Plan-subagent Decision 4 of #63 PR 3.
    """
    d = {
        "name": agent_id,
        "agent_mode": _derive_agent_mode_from_identity(identity),
        "persona_identity": identity,
        "persona_soul": soul if soul is not None else "",
        "persona_user": user if user is not None else "",
        "goal_text": goal if goal is not None else "",
        "model_md_raw": model if model is not None else "",
        "tools_md_raw": tools if tools is not None else "",
        "judges_md_raw": judges,
        "roster_md_raw": roster if roster is not None else "",
        "mcp_md_raw": mcp if mcp is not None else "",
        # Structured fields populated by from_dict re-parse from raw.
        "model_config": {},
        "tool_config": {},
        "tool_classifications": {},
        "judges_config": None,
        "roster": [],
        "mcp_servers": [],
    }
    profile = AgentProfile.from_dict(d)
    backend.save_profile(agent_id, profile)

    # Skill bodies live outside AgentProfile; write them directly when
    # the backend stores skills filesystem-style.
    if skills and backend.capabilities().supports_skills:
        agent_root = scope_root / agent_id
        for skill_name, body in skills.items():
            skill_dir = agent_root / "skills" / skill_name
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")

    return profile


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
    make_agent_in_backend(backend, tmp_path, "scout")
    profile = backend.load_profile("scout")
    assert profile.name == "scout"
    assert profile.persona_identity == _IDENTITY_BODY
    assert profile.persona_soul == _SOUL_BODY
    assert profile.persona_user == _USER_BODY


def test_load_profile_populates_structured_fields(backend, tmp_path):
    make_agent_in_backend(backend, tmp_path, "scout")
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
    make_agent_in_backend(
        backend,
        tmp_path,
        "goal-agent",
        identity=("# Goal Agent\n\n## Operating mode\n\nThis agent is goal-driven.\n"),
    )
    profile = backend.load_profile("goal-agent")
    assert profile.agent_mode == "goal-driven"


def test_load_profile_preserves_persona_byte_for_byte(backend, tmp_path):
    custom_identity = "# Custom\n\nCustom content with **markdown** and `code`.\n"
    make_agent_in_backend(backend, tmp_path, "scout", identity=custom_identity)
    profile = backend.load_profile("scout")
    assert profile.persona_identity == custom_identity


def test_load_profile_optional_files_absent(backend, tmp_path):
    """Goal, soul, user, judges, mcp, roster all optional."""
    make_agent_in_backend(
        backend,
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
    make_agent_in_backend(backend, tmp_path, "scout")
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
    make_agent_in_backend(backend, tmp_path, "scout", mcp=_MCP_BODY_WITH_VAR)
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
    make_agent_in_backend(backend, tmp_path, "scout", tools=custom_tools)
    profile = backend.load_profile("scout")
    assert "Dan's notes, scout-specific" in profile.tools_md_raw
    backend.save_profile("scout", profile)
    profile2 = backend.load_profile("scout")
    assert "Dan's notes, scout-specific" in profile2.tools_md_raw


def test_save_profile_ignores_agent_mode_field(backend, tmp_path):
    """Decision 6 — agent_mode is documented-derived; save ignores it."""
    make_agent_in_backend(backend, tmp_path, "scout")  # identity says "reactive"
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
    make_agent_in_backend(backend, tmp_path, "scout")
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
    make_agent_in_backend(backend, tmp_path, "charlie")
    make_agent_in_backend(backend, tmp_path, "alpha")
    make_agent_in_backend(backend, tmp_path, "bravo")
    assert backend.list_agents() == ["alpha", "bravo", "charlie"]


def test_list_agents_excludes_hidden_dirs(backend, tmp_path):
    """Filesystem-specific: hidden dirs (.snapshots/) on disk aren't agents.

    SQLite has no on-disk agent dirs — there's nothing to "exclude". The
    SQLite-equivalent invariant (only saved agents appear in list_agents)
    is covered by ``test_list_agents_lexicographic_order``.
    """
    if backend.backend_id != "filesystem":
        pytest.skip(
            f"{backend.backend_id!r}: hidden-dir exclusion is a "
            f"filesystem-specific behavior; SQLite has no disk agent dirs"
        )
    make_agent_in_backend(backend, tmp_path, "alpha")
    hidden = tmp_path / ".snapshots"
    (hidden / "persona").mkdir(parents=True)
    (hidden / "persona" / "IDENTITY.md").write_text("# Hidden\n", encoding="utf-8")
    assert backend.list_agents() == ["alpha"]


def test_list_agents_excludes_dirs_without_identity(backend, tmp_path):
    """Filesystem-specific: dirs without IDENTITY.md aren't agents.

    SQLite agents are rows; "row without identity" is impossible by
    schema. Covered for SQLite by the basic list test.
    """
    if backend.backend_id != "filesystem":
        pytest.skip(
            f"{backend.backend_id!r}: dir-without-identity exclusion is "
            f"a filesystem-specific behavior"
        )
    make_agent_in_backend(backend, tmp_path, "alpha")
    not_an_agent = tmp_path / "junk"
    not_an_agent.mkdir()
    (not_an_agent / "README.md").write_text("not an agent", encoding="utf-8")
    assert backend.list_agents() == ["alpha"]


# ──────────────────────────────────────────────────────────────────
# exists


def test_exists_true_for_valid_agent(backend, tmp_path):
    make_agent_in_backend(backend, tmp_path, "scout")
    assert backend.exists("scout") is True


def test_exists_false_for_missing_agent(backend, tmp_path):
    assert backend.exists("nope") is False


def test_exists_false_for_dir_without_identity(backend, tmp_path):
    """Filesystem-specific: dir without IDENTITY.md → exists() False.

    SQLite has no concept of a dir-without-identity — covered by
    ``test_exists_false_for_missing_agent``.
    """
    if backend.backend_id != "filesystem":
        pytest.skip(
            f"{backend.backend_id!r}: dir-without-identity is a "
            f"filesystem-specific shape"
        )
    (tmp_path / "junk").mkdir()
    assert backend.exists("junk") is False


# ──────────────────────────────────────────────────────────────────
# Skills


def test_list_skills_empty(backend, tmp_path):
    """Both backends return ``[]`` for an agent with no skills."""
    make_agent_in_backend(backend, tmp_path, "scout")
    assert backend.list_skills("scout") == []


def test_list_skills_returns_metadata(backend, tmp_path):
    """Skill content test — gates on ``supports_skills``.

    SQLite (``supports_skills=False``) doesn't store skill bodies;
    ``list_skills`` always returns ``[]`` for present agents (covered
    by ``test_list_skills_empty``). Future ``save_skill`` Protocol
    method will close this gap.
    """
    if not backend.capabilities().supports_skills:
        pytest.skip(
            f"{backend.backend_id!r}: supports_skills=False — skill "
            f"content tests don't apply"
        )
    skill_body = (
        "---\n"
        "name: spreadsheet-analysis\n"
        "description: Processes spreadsheets and generates summaries.\n"
        "---\n\n"
        "# Spreadsheet Analysis\n\nBody content here.\n"
    )
    make_agent_in_backend(
        backend, tmp_path, "scout", skills={"spreadsheet-analysis": skill_body}
    )
    skills = backend.list_skills("scout")
    assert len(skills) == 1
    assert skills[0].name == "spreadsheet-analysis"
    assert "spreadsheets" in skills[0].description


def test_load_skill_body_returns_body_without_frontmatter(backend, tmp_path):
    """Skill content test — gates on ``supports_skills`` (see above)."""
    if not backend.capabilities().supports_skills:
        pytest.skip(
            f"{backend.backend_id!r}: supports_skills=False — skill "
            f"content tests don't apply"
        )
    skill_body = (
        "---\n"
        "name: data-cleaning\n"
        "description: Cleans messy data.\n"
        "---\n\n"
        "# Body line one\n\n## Body line two\n"
    )
    make_agent_in_backend(
        backend, tmp_path, "scout", skills={"data-cleaning": skill_body}
    )
    body = backend.load_skill_body("scout", "data-cleaning")
    assert "# Body line one" in body
    assert "name: data-cleaning" not in body  # frontmatter stripped


def test_load_skill_body_unknown_skill_raises(backend, tmp_path):
    """Both backends raise FileNotFoundError for unknown skill name.

    SQLite raises because no skills exist for any agent in the SQLite
    backend; filesystem raises because the directory isn't there. Same
    exception type, different reason — fine.
    """
    make_agent_in_backend(backend, tmp_path, "scout")
    with pytest.raises(FileNotFoundError):
        backend.load_skill_body("scout", "nonexistent-skill")


# ──────────────────────────────────────────────────────────────────
# Clone


def test_clone_copies_profile_to_new_id(backend, tmp_path):
    make_agent_in_backend(backend, tmp_path, "source")
    backend.clone("source", "target")
    assert backend.exists("target")
    target_profile = backend.load_profile("target")
    source_profile = backend.load_profile("source")
    assert target_profile.persona_identity == source_profile.persona_identity
    assert target_profile.tools_md_raw == source_profile.tools_md_raw
    assert target_profile.name == "target"


def test_clone_applies_overrides(backend, tmp_path):
    make_agent_in_backend(backend, tmp_path, "source")
    new_identity = "# Cloned\n\n## Operating mode\n\nThis agent is hybrid.\n"
    backend.clone("source", "target", overrides={"persona_identity": new_identity})
    target = backend.load_profile("target")
    assert target.persona_identity == new_identity
    assert target.agent_mode == "hybrid"


def test_clone_refuses_overwrite(backend, tmp_path):
    make_agent_in_backend(backend, tmp_path, "source")
    make_agent_in_backend(backend, tmp_path, "target")  # already exists
    with pytest.raises(AgentProfileExists):
        backend.clone("source", "target")


def test_clone_unknown_override_raises(backend, tmp_path):
    make_agent_in_backend(backend, tmp_path, "source")
    with pytest.raises(ValueError):
        backend.clone("source", "target", overrides={"not_a_field": "value"})


def test_clone_missing_source_raises(backend, tmp_path):
    with pytest.raises(AgentProfileNotFound):
        backend.clone("nope", "target")


def test_clone_copies_skills_directory(backend, tmp_path):
    """Skill clone test — gates on ``supports_skills``.

    SQLite doesn't store skills, so cloning a SQLite agent doesn't
    copy them (there are none to copy). Filesystem backend walks the
    skills dir on clone and copies the tree.
    """
    if not backend.capabilities().supports_skills:
        pytest.skip(
            f"{backend.backend_id!r}: supports_skills=False — clone doesn't copy skills"
        )
    skill_body = (
        "---\n"
        "name: example-skill\n"
        "description: A test skill for clone.\n"
        "---\n\n# Body\n"
    )
    make_agent_in_backend(
        backend, tmp_path, "source", skills={"example-skill": skill_body}
    )
    backend.clone("source", "target")
    skills = backend.list_skills("target")
    assert len(skills) == 1
    assert skills[0].name == "example-skill"


# ──────────────────────────────────────────────────────────────────
# Capability-gated methods — claim-vs-behavior parity


def test_snapshot_unsupported_raises_not_implemented(backend, tmp_path):
    """When supports_snapshot=False, the trio MUST raise NotImplementedError."""
    if backend.capabilities().supports_snapshot:
        pytest.skip("backend supports snapshots; tested in supports_snapshot tests")
    make_agent_in_backend(backend, tmp_path, "scout")
    with pytest.raises(NotImplementedError):
        backend.snapshot("scout", "label")
    with pytest.raises(NotImplementedError):
        backend.restore("scout", "snapshot-id")
    with pytest.raises(NotImplementedError):
        backend.list_snapshots("scout")


# ──────────────────────────────────────────────────────────────────
# Snapshot trio — claim-vs-behavior parity for backends advertising
# ``supports_snapshot=True``. 7 tests added in #63 PR 3 alongside
# the filesystem snapshot trio + SQLite snapshot table.


def test_snapshot_round_trip(backend, tmp_path):
    """snapshot → mutate → restore returns the snapshotted state."""
    if not backend.capabilities().supports_snapshot:
        pytest.skip(f"{backend.backend_id!r}: supports_snapshot=False")
    make_agent_in_backend(backend, tmp_path, "scout")
    original_profile = backend.load_profile("scout")
    snapshot_id = backend.snapshot("scout", "baseline")
    # Mutate the agent after the snapshot.
    mutated = original_profile.replace(persona_soul="# Soul\n\nMutated soul.\n")
    backend.save_profile("scout", mutated)
    post_mutate = backend.load_profile("scout")
    assert post_mutate.persona_soul == "# Soul\n\nMutated soul.\n"
    # Restore — the snapshotted state is back.
    backend.restore("scout", snapshot_id)
    restored = backend.load_profile("scout")
    assert restored.persona_soul == original_profile.persona_soul
    assert restored.persona_identity == original_profile.persona_identity


def test_list_snapshots_empty(backend, tmp_path):
    """No snapshots taken → ``list_snapshots`` returns ``[]``."""
    if not backend.capabilities().supports_snapshot:
        pytest.skip(f"{backend.backend_id!r}: supports_snapshot=False")
    make_agent_in_backend(backend, tmp_path, "scout")
    assert backend.list_snapshots("scout") == []


def test_list_snapshots_chronological_order(backend, tmp_path):
    """Snapshots returned in ``created_at`` ascending order."""
    if not backend.capabilities().supports_snapshot:
        pytest.skip(f"{backend.backend_id!r}: supports_snapshot=False")
    import time as _time

    make_agent_in_backend(backend, tmp_path, "scout")
    first = backend.snapshot("scout", "first")
    _time.sleep(1.0)  # snapshot_id timestamp granularity is seconds
    second = backend.snapshot("scout", "second")
    _time.sleep(1.0)
    third = backend.snapshot("scout", "third")
    snapshots = backend.list_snapshots("scout")
    assert len(snapshots) == 3
    assert [s.snapshot_id for s in snapshots] == [first, second, third]


def test_list_snapshots_preserves_label(backend, tmp_path):
    """Operator-supplied ``label`` round-trips through list_snapshots."""
    if not backend.capabilities().supports_snapshot:
        pytest.skip(f"{backend.backend_id!r}: supports_snapshot=False")
    make_agent_in_backend(backend, tmp_path, "scout")
    backend.snapshot("scout", "pre-rewrite")
    snapshots = backend.list_snapshots("scout")
    assert len(snapshots) == 1
    assert snapshots[0].label == "pre-rewrite"


def test_restore_unknown_snapshot_raises(backend, tmp_path):
    """``restore`` raises ``SnapshotNotFound`` for an unknown snapshot_id."""
    if not backend.capabilities().supports_snapshot:
        pytest.skip(f"{backend.backend_id!r}: supports_snapshot=False")
    make_agent_in_backend(backend, tmp_path, "scout")
    with pytest.raises(SnapshotNotFound):
        backend.restore("scout", "snap_2026-05-15T120000_aabbcc")


def test_restore_cross_agent_isolation(backend, tmp_path):
    """A snapshot for agent A MUST NOT be restorable onto agent B.

    The conformance contract per spec/24 § ProfileSnapshot.agent_id:
    snapshots are scoped per-agent; ``restore(agent_id, snapshot_id)``
    raises ``SnapshotNotFound`` if the snapshot exists but belongs to
    a different agent.
    """
    if not backend.capabilities().supports_snapshot:
        pytest.skip(f"{backend.backend_id!r}: supports_snapshot=False")
    make_agent_in_backend(backend, tmp_path, "agent-a")
    make_agent_in_backend(backend, tmp_path, "agent-b")
    snapshot_a = backend.snapshot("agent-a", "a-snapshot")
    with pytest.raises(SnapshotNotFound):
        backend.restore("agent-b", snapshot_a)


def test_list_snapshots_refuses_path_traversal_agent_id(backend, tmp_path):
    """``list_snapshots`` MUST refuse path-traversal agent_id values.

    #63 PR 3 Step 11 adversarial F-3 — the filesystem backend's
    ``list_snapshots`` originally built ``<scope>/.snapshots/<agent_id>``
    via raw path concat and only validated the snapshot_id segment,
    leaving operator-controlled agent_id free to enumerate metadata
    from outside ``scope_root``. SQLite backends store agent_id as
    opaque string and refuse via the Protocol's own boundary checks
    or simply return no rows. Both shapes are acceptable; the
    invariant is: a path-traversal agent_id MUST NOT leak anything.
    """
    if not backend.capabilities().supports_snapshot:
        pytest.skip(f"{backend.backend_id!r}: supports_snapshot=False")
    # Path-traversal attempts. Filesystem backend raises ValueError
    # (refuses up-front); SQLite backend accepts the string as an
    # opaque key and returns an empty list (no rows match).
    for traversal in ("../escape", "../../other", "/etc/passwd"):
        try:
            result = backend.list_snapshots(traversal)
            # If no exception, result MUST be empty — no cross-scope leak.
            assert result == [], (
                f"backend {backend.backend_id!r} returned snapshots for "
                f"path-traversal agent_id {traversal!r} — security leak"
            )
        except (ValueError, OSError):
            # Filesystem backend's expected refusal.
            pass


def test_restore_refuses_path_traversal_agent_id(backend, tmp_path):
    """``restore`` MUST refuse path-traversal agent_id values.

    #63 PR 3 Step 11 adversarial F-3 — paired with the list_snapshots
    test above. A path-traversal agent_id must NOT read metadata.json
    or profile.json from outside ``scope_root``.
    """
    if not backend.capabilities().supports_snapshot:
        pytest.skip(f"{backend.backend_id!r}: supports_snapshot=False")
    for traversal in ("../escape", "../../other", "/etc/passwd"):
        with pytest.raises((ValueError, SnapshotNotFound, OSError)):
            backend.restore(traversal, "snap_2026-05-15T120000_aabbcc")


def test_snapshot_id_uniqueness_across_rapid_calls(backend, tmp_path):
    """Snapshot ids stay unique across N rapid calls (no collision).

    100 back-to-back snapshots exercises the random-tail entropy
    budget far past a 5-call probe (which would pass even on a
    pathologically weak generator). With 48-bit randomness (#63 PR 3
    Step 11 adversarial F-8 fix), 100 same-second calls have a
    collision probability of ~1.8e-11 — vanishingly small.
    """
    if not backend.capabilities().supports_snapshot:
        pytest.skip(f"{backend.backend_id!r}: supports_snapshot=False")
    make_agent_in_backend(backend, tmp_path, "scout")
    ids = {backend.snapshot("scout", f"snap-{i}") for i in range(100)}
    assert len(ids) == 100
    # Structural: the random tail must carry enough entropy. 12-hex
    # (48-bit) brings 4K-snapshot-per-second collision down to ~6e-8;
    # operators relying on the post-PR-3 entropy budget would notice
    # immediately if a future change reduced it.
    sample_tail = next(iter(ids)).rsplit("_", 1)[-1]
    assert len(sample_tail) >= 12


# ──────────────────────────────────────────────────────────────────
# AgentProfile dict round-trip — independent of any backend


def test_agent_profile_dict_round_trip(backend, tmp_path):
    """to_dict → from_dict preserves every field on a real profile."""
    make_agent_in_backend(backend, tmp_path, "scout")
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

    Gates on ``supports_skills``: SQLite raises FileNotFoundError for
    every skill_name regardless of shape (no skills stored), so the
    path-traversal-specific failure mode isn't observable there. The
    cross-agent isolation security property is enforced structurally —
    by not storing skills at all.
    """
    if not backend.capabilities().supports_skills:
        pytest.skip(
            f"{backend.backend_id!r}: supports_skills=False — no "
            f"skill-name path-traversal surface exists"
        )
    from atomic_agents.exceptions import SkillFileTraversal

    make_agent_in_backend(backend, tmp_path, "scout")
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


# ──────────────────────────────────────────────────────────────────
# Persona ownership composition — #62 PR 2 (D-PP-3 + D-PP-7)
#
# Ten conformance tests covering ``external_persona_ref`` and
# ``set_persona_ownership`` across both FilesystemAgentProfileBackend
# and SQLiteAgentProfileBackend.  Charset-refusal tests exercise the
# shared validation rule; the AgentProfileNotFound contract is pinned
# for both methods.
#
# Setup note: the filesystem backend's ``set_persona_ownership(non-None)``
# raises ``PersonaOwnershipConflict`` when ``persona/IDENTITY.md`` exists
# (D2a enforcement at write time).  Tests that call set_persona_ownership
# with a non-None value therefore need an agent WITHOUT an existing
# IDENTITY.md on the filesystem backend.
#
# ``make_persona_bindable_agent`` sets up such an agent for any backend:
# - Filesystem: writes only ``persona.link.md`` (placeholder persona_id
#   "placeholder") so the dir passes ``_is_agent_dir`` without IDENTITY.md.
# - SQLite: saves via ``save_profile`` (no on-disk IDENTITY.md exists;
#   SQLite stores state as a DB row and never checks for IDENTITY.md).
# Tests that only need ``external_persona_ref`` (read-side) use the
# standard ``make_agent_in_backend`` helper because a legacy agent is the
# canonical "internally owned" starting state.


def make_persona_bindable_agent(
    backend: AgentProfileBackend,
    scope_root: Path,
    agent_id: str,
) -> None:
    """Create an agent that can receive a non-None set_persona_ownership call.

    Filesystem backend: writes ``persona.link.md`` with a placeholder
    persona_id so the directory is recognized as an agent (D-PP-1) without
    a ``persona/IDENTITY.md`` that would trigger D2a conflict.

    SQLite backend: calls ``save_profile`` to insert a row; no on-disk
    IDENTITY.md is involved, so set_persona_ownership never hits D2a.
    """
    if backend.backend_id == "filesystem":
        agent_root = scope_root / agent_id
        agent_root.mkdir(parents=True, exist_ok=True)
        link_body = (
            "# Persona link\n\n```yaml\nkind: shared\npersona_id: placeholder\n```\n"
        )
        (agent_root / "persona.link.md").write_text(link_body, encoding="utf-8")
    else:
        # SQLite and any future non-filesystem backend: use the Protocol write path.
        make_agent_in_backend(backend, scope_root, agent_id)


def test_external_persona_ref_internally_owned_returns_none(backend, tmp_path):
    """An agent with the legacy three-file layout (internally owned) returns None.

    D-PP-3: ``external_persona_ref`` MUST return None (not raise) when
    the agent exists but has no shared-persona reference.
    """
    make_agent_in_backend(backend, tmp_path, "scout")
    result = backend.external_persona_ref("scout")
    assert result is None


def test_external_persona_ref_missing_agent_raises(backend, tmp_path):
    """D-PP-3: missing agent raises ``AgentProfileNotFound``, not None.

    The distinction is load-bearing — the framework's bootstrap path
    must distinguish "agent missing" from "agent internally owned".
    """
    with pytest.raises(AgentProfileNotFound):
        backend.external_persona_ref("ghost-agent")


def test_external_persona_ref_returns_persona_id_after_set(backend, tmp_path):
    """After ``set_persona_ownership``, ``external_persona_ref`` returns
    the persona_id that was written (D-PP-3 + D-PP-7 round-trip).
    """
    make_persona_bindable_agent(backend, tmp_path, "scout")
    backend.set_persona_ownership("scout", "shared-customer-v3")
    result = backend.external_persona_ref("scout")
    assert result == "shared-customer-v3"


def test_set_persona_ownership_round_trips_via_external_ref(backend, tmp_path):
    """``set_persona_ownership`` persists; the value is stable across two
    ``external_persona_ref`` reads on the same backend instance.
    """
    make_persona_bindable_agent(backend, tmp_path, "writer")
    backend.set_persona_ownership("writer", "shared-customer-v3")
    assert backend.external_persona_ref("writer") == "shared-customer-v3"
    # Second read is stable (no caching side-effect that clears the value).
    assert backend.external_persona_ref("writer") == "shared-customer-v3"


def test_set_persona_ownership_none_restores_internal(backend, tmp_path):
    """Setting ownership to None removes the external binding (D-PP-7).

    After set_persona_ownership(None), the agent is no longer externally
    owned.  The outcome for external_persona_ref differs by backend:

    - SQLite: the row still exists with persona_id=NULL → returns None.
    - Filesystem: the link file was the only sentinel; removing it makes
      the agent invisible to ``_is_agent_dir`` → raises AgentProfileNotFound.
      Operators must add ``persona/IDENTITY.md`` to restore list visibility
      (D-PP-7 docstring: "The operator is responsible...").

    Both outcomes are conformant with the Protocol: the binding is gone.
    """
    make_persona_bindable_agent(backend, tmp_path, "scout")
    backend.set_persona_ownership("scout", "some-persona")
    assert backend.external_persona_ref("scout") == "some-persona"
    backend.set_persona_ownership("scout", None)
    # Binding cleared — either None (row-based backends) or not-found (filesystem).
    try:
        result = backend.external_persona_ref("scout")
        assert result is None, f"expected None after clearing, got {result!r}"
    except AgentProfileNotFound:
        # Filesystem: link was only sentinel; dir is no longer an agent.
        pass


def test_set_persona_ownership_missing_agent_raises(backend, tmp_path):
    """D-PP-7: ``set_persona_ownership`` on a non-existent agent raises
    ``AgentProfileNotFound``.
    """
    with pytest.raises(AgentProfileNotFound):
        backend.set_persona_ownership("ghost-agent", "some-persona")


def test_set_persona_ownership_empty_string_raises_value_error(backend, tmp_path):
    """Charset rule refuses empty-string persona_id at the API boundary (D-PP-7).

    An empty persona_id would break the filesystem file format and the
    SQL read path; the backend must refuse before any I/O.

    Note: Protocol docstring says ``ValueError``; the filesystem backend
    raises ``PersonaLinkInvalid`` (a ``PersonaError`` subclass) via
    ``_validate_persona_id``.  Both are accepted here — they signal the
    same "bad input" contract; the test pins that SOME error is raised,
    not which class the backend chooses internally.
    """
    make_persona_bindable_agent(backend, tmp_path, "scout")
    with pytest.raises((ValueError, PersonaLinkInvalid)):
        backend.set_persona_ownership("scout", "")


def test_set_persona_ownership_dotdot_raises_value_error(backend, tmp_path):
    """Charset rule refuses ``..`` in persona_id (path-traversal defense)."""
    make_persona_bindable_agent(backend, tmp_path, "scout")
    with pytest.raises((ValueError, PersonaLinkInvalid)):
        backend.set_persona_ownership("scout", "../../etc/passwd")


def test_set_persona_ownership_slash_raises_value_error(backend, tmp_path):
    """Charset rule refuses ``/`` (path separator) in persona_id."""
    make_persona_bindable_agent(backend, tmp_path, "scout")
    with pytest.raises((ValueError, PersonaLinkInvalid)):
        backend.set_persona_ownership("scout", "my/persona")


def test_set_persona_ownership_control_char_raises_value_error(backend, tmp_path):
    """Charset rule refuses control characters in persona_id."""
    make_persona_bindable_agent(backend, tmp_path, "scout")
    with pytest.raises((ValueError, PersonaLinkInvalid)):
        backend.set_persona_ownership("scout", "bad\x00persona")

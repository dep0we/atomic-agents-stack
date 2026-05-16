"""Filesystem-specific tests for ``FilesystemAgentProfileBackend``.

Conformance tests in ``test_profile_protocol_conformance.py`` exercise
the Protocol contract that every backend must satisfy. THIS module
exercises the filesystem-specific behavior: on-disk path mapping,
hidden-directory exclusion, scope_root validation, registry dispatch,
operator-config redaction, and the cascade carve-out (Decision 5).

The conformance suite already covers byte-for-byte round-trip of all
raw fields and the security-critical Decision 1 ($VAR preservation)
— those tests stay there so future backends inherit them. This module
covers what's filesystem-only.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from atomic_agents.exceptions import BackendNotRegistered
from atomic_agents.profile import (
    FilesystemAgentProfileBackend,
    get_default_profile_backend,
    get_profile_backend,
    list_profile_backends,
)


# Reuse the agent-dir fixture helper from the conformance suite.
from tests.test_profile_protocol_conformance import make_agent_dir


# ──────────────────────────────────────────────────────────────────
# Constructor + scope_root validation


def test_scope_root_must_exist(tmp_path):
    missing = tmp_path / "does-not-exist"
    with pytest.raises(ValueError, match="does not exist"):
        FilesystemAgentProfileBackend(missing)


def test_scope_root_must_be_directory(tmp_path):
    file_path = tmp_path / "regular.txt"
    file_path.write_text("not a dir", encoding="utf-8")
    with pytest.raises(ValueError, match="not a directory"):
        FilesystemAgentProfileBackend(file_path)


def test_scope_root_property_readonly_after_construction(tmp_path):
    backend = FilesystemAgentProfileBackend(tmp_path)
    assert backend.scope_root == tmp_path
    # backend_id is a @property — instance-set attempts hit Python's
    # property-setter protocol (no setter defined).
    with pytest.raises(AttributeError):
        backend.backend_id = "spoof"  # type: ignore[misc]


# ──────────────────────────────────────────────────────────────────
# On-disk path mapping


def test_files_live_at_canonical_paths(tmp_path):
    make_agent_dir(tmp_path, "scout")
    # Mirrors agent.py:_load_config() expectations exactly
    assert (tmp_path / "scout" / "persona" / "IDENTITY.md").is_file()
    assert (tmp_path / "scout" / "persona" / "SOUL.md").is_file()
    assert (tmp_path / "scout" / "persona" / "USER.md").is_file()
    assert (tmp_path / "scout" / "tools.md").is_file()
    assert (tmp_path / "scout" / "model.md").is_file()
    assert (tmp_path / "scout" / "roster.md").is_file()
    assert (tmp_path / "scout" / "mcp.md").is_file()


def test_save_creates_canonical_paths(tmp_path):
    make_agent_dir(tmp_path, "source")
    backend = FilesystemAgentProfileBackend(tmp_path)
    profile = backend.load_profile("source")
    # Clone via save into a new agent dir
    new_profile = profile.replace(name="target")
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "persona").mkdir()
    # The empty target dir; save_profile should populate all paths.
    backend.save_profile("target", new_profile)
    # Files exist exactly where the bootstrap path expects them
    assert (tmp_path / "target" / "persona" / "IDENTITY.md").is_file()
    assert (tmp_path / "target" / "tools.md").is_file()
    assert (tmp_path / "target" / "model.md").is_file()


# ──────────────────────────────────────────────────────────────────
# Hidden-directory exclusion (matches log/lock arc's discipline)


def test_dot_snapshots_dir_excluded_from_list(tmp_path):
    make_agent_dir(tmp_path, "alpha")
    # Pre-create a .snapshots dir with a valid agent shape (it has
    # IDENTITY.md but is hidden — must not appear).
    snap_root = tmp_path / ".snapshots" / "alpha" / "snap-1"
    snap_root.mkdir(parents=True)
    (snap_root / "persona").mkdir()
    (snap_root / "persona" / "IDENTITY.md").write_text("# Snapshot\n", encoding="utf-8")

    backend = FilesystemAgentProfileBackend(tmp_path)
    assert backend.list_agents() == ["alpha"]


def test_other_hidden_prefixes_excluded(tmp_path):
    make_agent_dir(tmp_path, "alpha")
    # Common hidden-dir patterns: .git, .DS_Store-ish, .tmp.
    for hidden in (".git", ".tmp", ".cache"):
        hd = tmp_path / hidden
        hd.mkdir()
        # Even with IDENTITY.md it must not surface.
        (hd / "persona").mkdir()
        (hd / "persona" / "IDENTITY.md").write_text("# Hidden\n", encoding="utf-8")

    backend = FilesystemAgentProfileBackend(tmp_path)
    assert backend.list_agents() == ["alpha"]


# ──────────────────────────────────────────────────────────────────
# Path-traversal refusal — _agent_root() should refuse separators


def test_agent_id_with_backslash_refused(tmp_path):
    """Windows-shape path tokens stay refused — backslash attack shape."""
    backend = FilesystemAgentProfileBackend(tmp_path)
    with pytest.raises(ValueError, match="backslash"):
        backend.load_profile("nested\\path")


def test_agent_id_with_dotdot_refused(tmp_path):
    """Explicit ``..`` segments stay refused (path traversal)."""
    backend = FilesystemAgentProfileBackend(tmp_path)
    with pytest.raises(ValueError, match="\\.\\.|resolves outside"):
        backend.load_profile("../escape")
    with pytest.raises(ValueError, match="\\.\\.|resolves outside"):
        backend.load_profile("foo/../../escape")


def test_agent_id_with_slash_allowed_for_cascade(tmp_path):
    """Cascade agents have multi-segment identities under ``scope_root``.

    Per spec/06, a cascade-layout agent lives at
    ``<system>/projects/<project>/agents/<role>/`` — when ``scope_root``
    is the system root, the agent's identity is the full path beneath
    it. The PR 1 draft refused ``/`` in ``agent_id`` for safety, but
    that broke cascade integration. The relax-and-rely-on-relative_to
    fix (#63 PR 2 Decision 1) makes slash-containing IDs valid as long
    as they resolve under ``scope_root``.
    """
    # Build a cascade-shaped agent dir under scope_root
    instance_root = (
        tmp_path / "muse" / "projects" / "the-unfinished" / "agents" / "writer"
    )
    instance_root.mkdir(parents=True)
    (instance_root / "persona").mkdir()
    (instance_root / "persona" / "IDENTITY.md").write_text(
        "# Writer\n\n## Operating mode\n\nThis agent is reactive.\n",
        encoding="utf-8",
    )

    backend = FilesystemAgentProfileBackend(tmp_path)
    cascade_id = "muse/projects/the-unfinished/agents/writer"
    # Should NOT raise — cascade-shaped agent_id resolves correctly under scope_root
    assert backend.exists(cascade_id) is True
    profile = backend.load_profile(cascade_id)
    assert profile.name == cascade_id


def test_agent_id_starting_with_dot_refused(tmp_path):
    backend = FilesystemAgentProfileBackend(tmp_path)
    with pytest.raises(ValueError, match="'\\.'"):
        backend.load_profile(".hidden")


def test_empty_agent_id_refused(tmp_path):
    backend = FilesystemAgentProfileBackend(tmp_path)
    with pytest.raises(ValueError, match="must not be empty"):
        backend.load_profile("")


def test_exists_returns_false_for_invalid_id(tmp_path):
    """exists() must return False (NOT raise) for invalid ids."""
    backend = FilesystemAgentProfileBackend(tmp_path)
    assert backend.exists("../escape") is False
    assert backend.exists(".hidden") is False
    assert backend.exists("") is False


# ──────────────────────────────────────────────────────────────────
# Atomic save (atomic_write is exercised — fsync + temp + rename)


def test_save_is_atomic_temp_file_disappears(tmp_path):
    """No .tmp leftovers after a successful save."""
    make_agent_dir(tmp_path, "scout")
    backend = FilesystemAgentProfileBackend(tmp_path)
    profile = backend.load_profile("scout")
    backend.save_profile("scout", profile)
    # No .tmp files should remain in the agent dir
    tmps = list((tmp_path / "scout").rglob(".*.tmp"))
    assert tmps == []


# ──────────────────────────────────────────────────────────────────
# Registry + factory


def test_registry_resolves_filesystem(tmp_path):
    cls = get_profile_backend("filesystem")
    assert cls is FilesystemAgentProfileBackend


def test_registry_lists_filesystem_at_minimum():
    backends = list_profile_backends()
    assert "filesystem" in backends


def test_default_factory_returns_filesystem(tmp_path):
    """No env var set → filesystem default."""
    # Clear env to be deterministic
    old = os.environ.pop("ATOMIC_AGENTS_PROFILE_BACKEND", None)
    try:
        backend = get_default_profile_backend(tmp_path)
        assert isinstance(backend, FilesystemAgentProfileBackend)
        assert backend.scope_root == tmp_path
    finally:
        if old is not None:
            os.environ["ATOMIC_AGENTS_PROFILE_BACKEND"] = old


def test_default_factory_unknown_backend_id_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("ATOMIC_AGENTS_PROFILE_BACKEND", "totally-not-real")
    with pytest.raises(BackendNotRegistered, match="totally-not-real"):
        get_default_profile_backend(tmp_path)


def test_default_factory_credential_redaction(tmp_path, monkeypatch):
    """Decision 1-ish for the factory: URLs accidentally pasted into the
    BACKEND env var must have their credentials redacted in the error
    message."""
    monkeypatch.setenv(
        "ATOMIC_AGENTS_PROFILE_BACKEND",
        "postgres://user:secretpass@host:5432/db",
    )
    with pytest.raises(BackendNotRegistered) as exc_info:
        get_default_profile_backend(tmp_path)
    err_text = str(exc_info.value)
    assert "secretpass" not in err_text
    assert "user:secretpass" not in err_text
    # Scheme is allowed to surface
    assert "postgres" in err_text


def test_default_factory_long_backend_id_truncated(tmp_path, monkeypatch):
    """Pathological long value gets truncated to bound the echoed string."""
    long_val = "x" * 200
    monkeypatch.setenv("ATOMIC_AGENTS_PROFILE_BACKEND", long_val)
    with pytest.raises(BackendNotRegistered) as exc_info:
        get_default_profile_backend(tmp_path)
    err_text = str(exc_info.value)
    # The full 200-char value MUST NOT appear; ellipsis truncation kicks in
    assert "x" * 200 not in err_text
    assert "..." in err_text


# ──────────────────────────────────────────────────────────────────
# Cascade carve-out (Decision 5) — load merges, save writes instance only


def _make_cascaded_layout(scope_root: Path) -> tuple[Path, Path, Path]:
    """Build a minimal cascade-shape:

        <system>/
            roles/<role>/PROMPT.md + tools.md + model.md
            projects/<project>/judges.md (optional)
                agents/<role>/persona/IDENTITY.md (instance)

    Returns (instance_root, role_root, project_root).
    """
    system = scope_root / "system"
    role_root = system / "roles" / "editor"
    project_root = system / "projects" / "novella"
    instance_root = project_root / "agents" / "editor"

    role_root.mkdir(parents=True)
    (role_root / "PROMPT.md").write_text("# Editor role\n", encoding="utf-8")
    (role_root / "tools.md").write_text(
        "# Role tools\n\n## Read paths\n\n- ~/role/data\n",
        encoding="utf-8",
    )
    (role_root / "model.md").write_text(
        "# Role model\n\n## Default model\n\nclaude-sonnet-4-6-20260101\n",
        encoding="utf-8",
    )

    project_root.mkdir(parents=True)

    instance_root.mkdir(parents=True)
    (instance_root / "persona").mkdir()
    (instance_root / "persona" / "IDENTITY.md").write_text(
        "# Editor instance\n\n## Operating mode\n\nThis agent is reactive.\n",
        encoding="utf-8",
    )
    return instance_root, role_root, project_root


def test_cascade_load_picks_up_role_tools(tmp_path):
    """For a cascaded agent without instance/tools.md, ``tools_md_raw``
    is the role layer's text."""
    _make_cascaded_layout(tmp_path)
    backend = FilesystemAgentProfileBackend(
        tmp_path / "system" / "projects" / "novella" / "agents"
    )
    profile = backend.load_profile("editor")
    assert "Role tools" in profile.tools_md_raw
    assert "Role model" in profile.model_md_raw


def test_cascade_save_writes_instance_only_for_role_tools(tmp_path):
    """Save MUST NOT mutate the role-layer tools.md when the operator
    didn't author an instance/tools.md. The role layer is shared."""
    instance_root, role_root, _ = _make_cascaded_layout(tmp_path)
    role_tools_path = role_root / "tools.md"
    role_tools_before = role_tools_path.read_text(encoding="utf-8")

    backend = FilesystemAgentProfileBackend(
        tmp_path / "system" / "projects" / "novella" / "agents"
    )
    profile = backend.load_profile("editor")
    # Mutate the field as if an operator edited it
    altered = profile.replace(tools_md_raw="# DIFFERENT\n")
    backend.save_profile("editor", altered)

    # Role tools.md MUST be unchanged
    assert role_tools_path.read_text(encoding="utf-8") == role_tools_before


def test_cascade_save_writes_to_instance_when_instance_file_exists(tmp_path):
    """When the instance has its own tools.md, save targets that file."""
    instance_root, role_root, _ = _make_cascaded_layout(tmp_path)
    instance_tools = instance_root / "tools.md"
    instance_tools.write_text(
        "# Instance override\n\n## Read paths\n\n- ~/instance/data\n",
        encoding="utf-8",
    )

    backend = FilesystemAgentProfileBackend(
        tmp_path / "system" / "projects" / "novella" / "agents"
    )
    profile = backend.load_profile("editor")
    altered = profile.replace(tools_md_raw="# UPDATED INSTANCE\n")
    backend.save_profile("editor", altered)

    # Instance tools.md WAS updated
    assert instance_tools.read_text(encoding="utf-8") == "# UPDATED INSTANCE\n"
    # Role tools.md was NOT
    assert "Role tools" in (role_root / "tools.md").read_text(encoding="utf-8")


def test_cascade_floor_judges_md_does_not_materialize_instance_shadow(tmp_path):
    """Step 11 adversarial Finding P1#1 regression test.

    When a cascaded agent inherits ``judges.md`` from the project floor
    WITHOUT authoring its own instance-layer judges.md, a load → save
    round-trip MUST NOT materialize a ghost instance/judges.md file
    containing a copy of the floor text. Such a ghost would shadow the
    floor forever — future floor changes would be silently ignored.

    The fix (filesystem.py ``load_profile``): ``judges_md_raw`` is
    None when the instance file is absent, regardless of whether a
    floor exists. The structured ``judges_config`` still carries the
    merged effective config via ``load_judges_config``.
    """
    instance_root, _, project_root = _make_cascaded_layout(tmp_path)
    # Floor judges.md with a meaningful policy
    floor_judges = project_root / "judges.md"
    floor_judges.write_text(
        "# Project floor judges\n\n"
        "```yaml\n"
        "default_backend: rules\n"
        "class_policy:\n"
        "  read_only: bypass\n"
        "  reversible_write: judge_required\n"
        "  external_side_effect: judge_required\n"
        "  high_risk: escalate\n"
        "```\n",
        encoding="utf-8",
    )
    # Instance has NO judges.md
    instance_judges = instance_root / "judges.md"
    assert not instance_judges.exists()

    backend = FilesystemAgentProfileBackend(
        tmp_path / "system" / "projects" / "novella" / "agents"
    )
    profile = backend.load_profile("editor")

    # judges_md_raw must be None — represents instance layer only.
    # Floor inheritance is invisible at the raw_text level (by design).
    assert profile.judges_md_raw is None
    # But judges_config IS populated — load_judges_config merges the floor.
    assert profile.judges_config is not None

    # Round-trip: save → load should NOT materialize instance/judges.md
    backend.save_profile("editor", profile)
    assert not instance_judges.exists(), (
        "Ghost instance judges.md materialized — the floor would be "
        "silently shadowed from this point forward"
    )
    # Floor file untouched
    assert floor_judges.read_text(encoding="utf-8").startswith("# Project floor judges")


def test_from_dict_raises_loud_when_judges_config_is_dict_shape(tmp_path):
    """Step 11 adversarial Finding P1#2 regression test.

    When a database backend round-trips an AgentProfile via to_dict() →
    JSONB store → retrieve → from_dict(), the retrieved
    ``judges_config`` value will be a plain dict (not a JudgesConfig
    instance). The pre-Step-11 code silently dropped it to None,
    losing the operator's judge policy without warning. The fix:
    raise TypeError loudly so the failure surfaces at deserialization
    rather than silently corrupting the agent's runtime judge policy.

    The expected resolution path is: PR 3 DB backend persists
    judges_md_raw alongside structured columns (the typed-shadow +
    raw-text design from Decision 1), OR a future JudgesConfig.from_dict
    implementation reconstructs the dataclass from the dict shape.
    """
    from atomic_agents.profile import AgentProfile

    # Build a dict where judges_md_raw is absent but judges_config
    # is present as a dict (simulating DB-only round-trip).
    d = {
        "name": "scout",
        "agent_mode": "reactive",
        "model_config": {},
        "tool_config": {},
        "tool_classifications": {},
        "judges_config": {  # dict shape — what _judges_config_to_dict produces
            "default_backend": "rules",
            "default_model": None,
            "timeout_ms": 5000,
        },
        "roster": [],
        "mcp_servers": [],
        "persona_identity": "# Scout\n",
        "persona_soul": "",
        "persona_user": "",
        "goal_text": "",
        "model_md_raw": "",
        "tools_md_raw": "",
        "judges_md_raw": None,  # critical: no raw text alongside structured
        "roster_md_raw": "",
        "mcp_md_raw": "",
    }
    with pytest.raises(TypeError, match="judges_md_raw"):
        AgentProfile.from_dict(d)

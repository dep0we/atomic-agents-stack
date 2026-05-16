"""FilesystemAgentProfileBackend — directory-tree reference implementation.

This is the default backend for single-host deployments. It wraps the same
on-disk shape ``AtomicAgent.__init__`` has used since the framework's first
release: each agent is one directory under ``agents_root`` with its config
files (``model.md``, ``tools.md``, ``judges.md``, ``roster.md``, ``mcp.md``,
``goal.md``), persona bodies (``persona/IDENTITY.md|SOUL.md|USER.md``), and
skill subdirectories (``skills/<name>/SKILL.md``).

Three surface promises hold across PR 1 → PR 2:

1. **Byte-for-byte file preservation.** ``save_profile()`` writes raw text
   fields verbatim via ``_io.atomic_write``. External scripts that read or
   edit the on-disk markdown keep working unchanged. No structured-form
   render path is exercised on save; the raw text IS the source of truth
   (see spec/24 §"Decision 1").
2. **Backward-compatible read.** ``load_profile()`` uses the existing
   parsers (``_model.parse_model_md``, ``_tools.parse_tools_md``,
   ``judges_md.load_judges_config``, etc.) so the structured forms match
   exactly what ``AtomicAgent.__init__`` produces today. PR 2 wires the
   bootstrap path through ``load_profile()`` knowing the structured shapes
   are byte-for-byte identical.
3. **Cascade carve-out.** For cascaded agents,
   ``tools_md_raw``/``model_md_raw`` carry the post-merge text from
   ``_cascade.resolve_tools_md(cascade)`` /
   ``_cascade.resolve_model_md(cascade)``. ``save_profile()`` writes ONLY
   instance-layer files; project-floor and role-layer files are read
   paths only.

Snapshot/restore are NOT implemented in PR 1 (``supports_snapshot=False``);
the trio raises ``NotImplementedError``. PR 3 ships snapshot support along
with the second reference impl (likely SQLite, where snapshots are a row
in a snapshots table — much simpler than filesystem dir copies).

Scope: bound at construction. ``FilesystemAgentProfileBackend(agents_root)``
operates on subdirectories of ``agents_root``. Agents are flat siblings;
there is no nested-namespace concept like ``LockBackend.scope()``.

Thread-safety: each method opens / writes / closes its own file handles
inside its own call. Concurrent ``save_profile`` calls against the same
agent rely on ``_io.atomic_write``'s temp+rename atomicity — the last
writer wins, but neither caller sees a partially-written file.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from .._io import atomic_write
from .._cascade import detect_cascade, resolve_model_md, resolve_tools_md
from .._model import parse_model_md_text
from .._roster import parse_roster_md_text
from .._tools import (
    parse_tool_classifications_text,
    parse_tools_md_text,
)
from ..exceptions import (
    AgentProfileExists,
    AgentProfileNotFound,
    MCPServerConnectFailed,
)
from ..goal import parse_agent_mode
from ..judges_md import load_judges_config
from ..mcp import parse_mcp_md
from ..skills import (
    SKILL_ENTRY_POINT,
    SkillManifest,
    discover_skills,
    load_skill_body,
)
from .types import (
    AgentProfile,
    ProfileCapabilities,
    ProfileSnapshot,
)


# Hidden directory prefix — ``list_agents()`` skips entries starting with
# this so backend-internal storage (``.snapshots/``, future ``.tmp/``)
# doesn't surface as agents.
_HIDDEN_PREFIX = "."

# Sentinel that distinguishes an agent directory from a non-agent directory.
# Mirrors ``doctor.check_vault``'s requirement at ``doctor.py:386`` —
# IDENTITY.md is the load-bearing identity file every agent has.
_IDENTITY_RELATIVE = Path("persona") / "IDENTITY.md"
_SOUL_RELATIVE = Path("persona") / "SOUL.md"
_USER_RELATIVE = Path("persona") / "USER.md"


class FilesystemAgentProfileBackend:
    """Directory-tree AgentProfileBackend — preserves the legacy on-disk shape.

    Conforms to the ``AgentProfileBackend`` Protocol. Constructed once
    per process; the ``scope_root`` is the directory under which every
    agent lives as a sibling subdirectory. PR 2 wires the framework's
    default profile backend at module import via
    ``get_default_profile_backend(agents_root)``.

    Args:
        scope_root: directory containing agent subdirectories. MUST
            exist at construction time; the constructor raises
            ``ValueError`` otherwise. (Filesystem backends operate on
            existing directories; database backends create their schema
            on first use, but a missing scope_root for a filesystem
            backend is almost always operator error.)
    """

    # ``backend_id`` is a ``@property`` (not a class attribute) for
    # parity with the LogBackend / LockBackend / LLMBackend Protocol
    # patterns. Property form prevents instance-level mutation —
    # ``b.backend_id = "spoof"`` would silently succeed against a class
    # attribute and desynchronize diagnostic logging from registry
    # lookups.
    @property
    def backend_id(self) -> str:
        return "filesystem"

    def __init__(self, scope_root: Path) -> None:
        scope = Path(scope_root)
        if not scope.is_dir():
            raise ValueError(
                f"FilesystemAgentProfileBackend scope_root does not exist "
                f"or is not a directory: {scope}"
            )
        self._scope_root = scope

    @property
    def scope_root(self) -> Path:
        """The agents_root this backend is bound to. Read-only after construction."""
        return self._scope_root

    # ────────────────────────────────────────────────────────────
    # Path helpers

    def _agent_root(self, agent_id: str) -> Path:
        """Resolve agent directory; raises on path-traversal attempts.

        Filesystem backends MUST validate ``agent_id`` cannot escape
        ``scope_root``. ``"../other-system/agent"`` is the obvious
        attack shape; refuse via the standard relative-to check rather
        than a regex blocklist.
        """
        if not agent_id:
            raise ValueError("agent_id must not be empty")
        # Forbid path separators and parent-dir tokens up front so the
        # error message is actionable. The ``relative_to`` check below
        # is the belt-and-suspenders.
        if "/" in agent_id or "\\" in agent_id or agent_id.startswith("."):
            raise ValueError(
                f"agent_id {agent_id!r} contains a path separator or "
                f"starts with '.' — filesystem backend requires plain "
                f"directory names"
            )
        candidate = (self._scope_root / agent_id).resolve()
        try:
            candidate.relative_to(self._scope_root.resolve())
        except ValueError as exc:
            raise ValueError(
                f"agent_id {agent_id!r} resolves outside scope_root {self._scope_root}"
            ) from exc
        return self._scope_root / agent_id

    def _identity_path(self, agent_root: Path) -> Path:
        return agent_root / _IDENTITY_RELATIVE

    # ────────────────────────────────────────────────────────────
    # Core read

    def load_profile(self, agent_id: str) -> AgentProfile:
        """Read every config file + persona body and return the assembled profile.

        Cascade-aware: when the agent path matches the
        ``<system>/projects/<project>/agents/<role>`` pattern,
        ``tools_md_raw`` and ``model_md_raw`` carry the merged text
        from the existing ``_cascade.resolve_*`` functions. The
        instance-layer judges.md is parsed in cascade-aware mode via
        ``load_judges_config(agent_root, cascade, tools_md_text=...)``
        so the project-floor strictness check fires the same way it
        does in ``AtomicAgent.__init__``.
        """
        agent_root = self._agent_root(agent_id)
        identity_path = self._identity_path(agent_root)
        if not identity_path.is_file():
            raise AgentProfileNotFound(
                f"agent {agent_id!r} not found at {agent_root}: "
                f"persona/IDENTITY.md is missing or not a file"
            )

        cascade = detect_cascade(agent_root)

        # ── Raw text + structured forms for cascade-aware files ──
        if cascade is not None:
            tools_path, tools_md_raw = resolve_tools_md(cascade)
            model_path = resolve_model_md(cascade)
            model_md_raw = model_path.read_text(encoding="utf-8") if model_path else ""
        else:
            tools_path = agent_root / "tools.md"
            tools_md_raw = (
                tools_path.read_text(encoding="utf-8") if tools_path.is_file() else ""
            )
            model_path_candidate = agent_root / "model.md"
            model_md_raw = (
                model_path_candidate.read_text(encoding="utf-8")
                if model_path_candidate.is_file()
                else ""
            )

        model_config = parse_model_md_text(model_md_raw)
        tool_config = parse_tools_md_text(tools_md_raw)
        tool_classifications = parse_tool_classifications_text(tools_md_raw)

        # ── judges.md — cascade-aware via load_judges_config ──
        judges_md_path = agent_root / "judges.md"
        floor_path = cascade.project_root / "judges.md" if cascade is not None else None
        if judges_md_path.is_file():
            judges_md_raw: str | None = judges_md_path.read_text(encoding="utf-8")
        elif floor_path is not None and floor_path.is_file():
            # Cascaded agent inheriting the floor only — no instance file.
            # Mirror the behavior of load_judges_config which returns the
            # floor as the effective config. Raw text on the profile
            # represents what the agent sees after merging — we use the
            # floor text since that's what the runtime composes with.
            judges_md_raw = floor_path.read_text(encoding="utf-8")
        else:
            judges_md_raw = None
        judges_config = load_judges_config(
            agent_root, cascade, tools_md_text=tools_md_raw
        )

        # ── roster.md ──
        roster_md_path = agent_root / "roster.md"
        roster_md_raw = (
            roster_md_path.read_text(encoding="utf-8")
            if roster_md_path.is_file()
            else ""
        )
        roster = parse_roster_md_text(roster_md_raw)

        # ── mcp.md ──
        mcp_md_path = agent_root / "mcp.md"
        mcp_md_raw = (
            mcp_md_path.read_text(encoding="utf-8") if mcp_md_path.is_file() else ""
        )
        # Pass read_paths so path-traversal validation matches the live
        # bootstrap behavior. parse_mcp_md raises if env var refs can't be
        # resolved; for load purposes we accept that single failure mode
        # so a profile referencing dev-only env vars is still inspectable
        # in a fresh process. The raw text on the profile is preserved
        # so the operator can write back without losing the references.
        #
        # NARROW catch: only ``MCPServerConnectFailed`` — the env-var-
        # resolution failure shape parse_mcp_md raises at line 562-565.
        # ``PathTraversalError`` (mcp.md server arg escaping read_paths)
        # is a security finding and MUST propagate — silently returning
        # ``mcp_servers = []`` would mask malicious server declarations
        # at load time. Pre-#63-PR-1-review-pass this was ``except
        # Exception`` which swallowed both — Step 9 pre-landing review
        # finding F-3.
        try:
            mcp_servers = parse_mcp_md(
                mcp_md_path, read_paths=tool_config.get("read_paths")
            )
        except MCPServerConnectFailed:
            mcp_servers = []

        # ── persona/IDENTITY.md, SOUL.md, USER.md — raw text ──
        persona_identity = identity_path.read_text(encoding="utf-8")
        soul_path = agent_root / _SOUL_RELATIVE
        persona_soul = (
            soul_path.read_text(encoding="utf-8") if soul_path.is_file() else ""
        )
        user_path = agent_root / _USER_RELATIVE
        persona_user = (
            user_path.read_text(encoding="utf-8") if user_path.is_file() else ""
        )

        # ── goal.md — raw text (GoalManager handles the structured path) ──
        goal_path = agent_root / "goal.md"
        goal_text = goal_path.read_text(encoding="utf-8") if goal_path.is_file() else ""

        # ── agent_mode — derived (Decision 6) ──
        agent_mode = parse_agent_mode(identity_path)

        return AgentProfile(
            name=agent_id,
            agent_mode=agent_mode,
            model_config=model_config,
            tool_config=tool_config,
            tool_classifications=tool_classifications,
            judges_config=judges_config,
            roster=roster,
            mcp_servers=mcp_servers,
            persona_identity=persona_identity,
            persona_soul=persona_soul,
            persona_user=persona_user,
            goal_text=goal_text,
            model_md_raw=model_md_raw,
            tools_md_raw=tools_md_raw,
            judges_md_raw=judges_md_raw,
            roster_md_raw=roster_md_raw,
            mcp_md_raw=mcp_md_raw,
        )

    # ────────────────────────────────────────────────────────────
    # Core write

    def save_profile(self, agent_id: str, profile: AgentProfile) -> None:
        """Write every raw-text field to its on-disk path via ``_io.atomic_write``.

        Cascade carve-out: for cascaded agents, save writes ONLY the
        instance-layer files. The role layer's tools.md / model.md and
        the project-floor judges.md are NOT touched. This matches the
        runtime's bootstrap split — the role and project layers are
        shared across instances and operators editing them via
        ``save_profile`` would corrupt every other instance.
        """
        agent_root = self._agent_root(agent_id)
        agent_root.mkdir(parents=True, exist_ok=True)

        cascade = detect_cascade(agent_root)

        # ── Persona — always instance-layer ──
        atomic_write(agent_root / _IDENTITY_RELATIVE, profile.persona_identity)
        # SOUL.md and USER.md are optional: write only when non-empty
        # so save→load→save round-trips don't materialize empty files
        # the operator never authored.
        if profile.persona_soul:
            atomic_write(agent_root / _SOUL_RELATIVE, profile.persona_soul)
        else:
            _unlink_if_exists(agent_root / _SOUL_RELATIVE)
        if profile.persona_user:
            atomic_write(agent_root / _USER_RELATIVE, profile.persona_user)
        else:
            _unlink_if_exists(agent_root / _USER_RELATIVE)

        # ── goal.md — instance-layer; absent when empty ──
        if profile.goal_text:
            atomic_write(agent_root / "goal.md", profile.goal_text)
        else:
            _unlink_if_exists(agent_root / "goal.md")

        # ── model.md — cascade-aware: write only when instance has its own ──
        if cascade is not None:
            instance_model = cascade.instance_root / "model.md"
            # Write the instance file only when the operator has authored
            # one (instance file exists OR profile carries non-empty raw
            # text that differs from the role file). Stay conservative:
            # only write to the instance layer when it already exists.
            # Operators wanting to override the role layer should drop
            # an instance file first, then save_profile.
            if instance_model.is_file() and profile.model_md_raw:
                atomic_write(instance_model, profile.model_md_raw)
        else:
            if profile.model_md_raw:
                atomic_write(agent_root / "model.md", profile.model_md_raw)
            else:
                _unlink_if_exists(agent_root / "model.md")

        # ── tools.md — cascade-aware: write only the instance layer ──
        if cascade is not None:
            instance_tools = cascade.instance_root / "tools.md"
            instance_override = cascade.instance_root / "tools.override.md"
            # Mirror the model.md conservative behavior: write to whichever
            # instance file already exists. tools.override.md takes
            # precedence per resolve_tools_md (additive merge), so writes
            # land there when present; falls back to instance/tools.md.
            if instance_override.is_file() and profile.tools_md_raw:
                atomic_write(instance_override, profile.tools_md_raw)
            elif instance_tools.is_file() and profile.tools_md_raw:
                atomic_write(instance_tools, profile.tools_md_raw)
        else:
            if profile.tools_md_raw:
                atomic_write(agent_root / "tools.md", profile.tools_md_raw)
            else:
                _unlink_if_exists(agent_root / "tools.md")

        # ── judges.md — cascade-aware: write only the instance layer ──
        # Project floor at <project>/judges.md is read-only from the
        # profile backend's perspective.
        instance_judges = (
            cascade.instance_root if cascade is not None else agent_root
        ) / "judges.md"
        if profile.judges_md_raw is not None:
            atomic_write(instance_judges, profile.judges_md_raw)
        else:
            _unlink_if_exists(instance_judges)

        # ── roster.md — instance-layer ──
        if profile.roster_md_raw:
            atomic_write(agent_root / "roster.md", profile.roster_md_raw)
        else:
            _unlink_if_exists(agent_root / "roster.md")

        # ── mcp.md — instance-layer; PRESERVES $VAR refs (Decision 1) ──
        if profile.mcp_md_raw:
            atomic_write(agent_root / "mcp.md", profile.mcp_md_raw)
        else:
            _unlink_if_exists(agent_root / "mcp.md")

    # ────────────────────────────────────────────────────────────
    # Enumeration

    def list_agents(self) -> list[str]:
        """Return subdirs of ``scope_root`` with persona/IDENTITY.md, lex order."""
        if not self._scope_root.is_dir():
            return []
        agents: list[str] = []
        for entry in sorted(self._scope_root.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name.startswith(_HIDDEN_PREFIX):
                # Skip hidden dirs (.snapshots, .tmp, .git, etc.).
                continue
            if (entry / _IDENTITY_RELATIVE).is_file():
                agents.append(entry.name)
        return agents

    def exists(self, agent_id: str) -> bool:
        """True when the agent dir + persona/IDENTITY.md sentinel exists."""
        try:
            agent_root = self._agent_root(agent_id)
        except ValueError:
            return False
        return (agent_root / _IDENTITY_RELATIVE).is_file()

    # ────────────────────────────────────────────────────────────
    # Skills — delegate to existing skills.py

    def list_skills(self, agent_id: str) -> list[SkillManifest]:
        """Discover ``<agent_root>/skills/*/SKILL.md`` and return manifests."""
        agent_root = self._agent_root(agent_id)
        if not (agent_root / _IDENTITY_RELATIVE).is_file():
            raise AgentProfileNotFound(f"agent {agent_id!r} not found at {agent_root}")
        return discover_skills(agent_root)

    def load_skill_body(self, agent_id: str, skill_name: str) -> str:
        """Read the named skill's SKILL.md body (frontmatter stripped)."""
        agent_root = self._agent_root(agent_id)
        if not (agent_root / _IDENTITY_RELATIVE).is_file():
            raise AgentProfileNotFound(f"agent {agent_id!r} not found at {agent_root}")
        skill_dir = agent_root / "skills" / skill_name
        skill_md = skill_dir / SKILL_ENTRY_POINT
        if not skill_md.is_file():
            raise FileNotFoundError(
                f"skill {skill_name!r} not found for agent {agent_id!r} at {skill_md}"
            )
        # Build a minimal manifest just to satisfy load_skill_body's
        # signature — discover_skills() does fuller validation, but for
        # loading the body we only need the path.
        manifest = SkillManifest(
            name=skill_name,
            description="",
            when_to_use=None,
            skill_dir=skill_dir,
            skill_md_path=skill_md,
            body_lines=0,
        )
        return load_skill_body(manifest)

    # ────────────────────────────────────────────────────────────
    # Clone

    def clone(
        self,
        source_id: str,
        target_id: str,
        overrides: dict[str, Any] | None = None,
    ) -> None:
        """Copy ``source_id``'s profile + skills to ``target_id``,
        applying overrides at write time.

        Implementation: ``load_profile(source)`` → apply overrides via
        ``replace`` → ``save_profile(target)``, then copy the skills
        directory tree if present.
        """
        if not self.exists(source_id):
            raise AgentProfileNotFound(f"clone source {source_id!r} does not exist")
        if self.exists(target_id):
            raise AgentProfileExists(
                f"clone target {target_id!r} already exists; "
                f"use save_profile() to overwrite intentionally"
            )

        source_profile = self.load_profile(source_id)

        # Apply overrides via dataclasses.replace — each key MUST be
        # an AgentProfile field name; unknown keys raise TypeError
        # which we re-raise as ValueError for clearer operator error
        # messages.
        if overrides:
            unknown = set(overrides.keys()) - set(
                AgentProfile.__dataclass_fields__.keys()
            )
            if unknown:
                raise ValueError(
                    f"clone overrides contain unknown AgentProfile field "
                    f"names: {sorted(unknown)}. Known fields: "
                    f"{sorted(AgentProfile.__dataclass_fields__.keys())}"
                )
            new_profile = replace(source_profile, name=target_id, **overrides)
        else:
            new_profile = replace(source_profile, name=target_id)

        # save_profile creates the agent_root if needed and writes raw
        # text fields verbatim.
        self.save_profile(target_id, new_profile)

        # Copy the skills directory tree if present — skills are not
        # part of AgentProfile (Decision 2) so they need a separate
        # copy step.
        source_skills = self._agent_root(source_id) / "skills"
        if source_skills.is_dir():
            target_skills = self._agent_root(target_id) / "skills"
            _copy_dir_tree(source_skills, target_skills)

    # ────────────────────────────────────────────────────────────
    # Snapshot — DEFERRED to PR 3 (Decision 3)

    def snapshot(self, agent_id: str, label: str) -> str:
        """NOT IMPLEMENTED in PR 1 — see Decision 3 in spec/24."""
        raise NotImplementedError(
            "FilesystemAgentProfileBackend does not support snapshots in "
            "#63 PR 1; capabilities().supports_snapshot is False. PR 3 "
            "ships snapshot implementation alongside the second reference "
            "backend."
        )

    def restore(self, agent_id: str, snapshot_id: str) -> None:
        """NOT IMPLEMENTED in PR 1 — see Decision 3 in spec/24."""
        raise NotImplementedError(
            "FilesystemAgentProfileBackend does not support snapshot "
            "restore in #63 PR 1; capabilities().supports_snapshot is "
            "False. PR 3 ships restore implementation alongside the "
            "second reference backend."
        )

    def list_snapshots(self, agent_id: str) -> list[ProfileSnapshot]:
        """NOT IMPLEMENTED in PR 1 — see Decision 3 in spec/24."""
        raise NotImplementedError(
            "FilesystemAgentProfileBackend does not list snapshots in "
            "#63 PR 1; capabilities().supports_snapshot is False. PR 3 "
            "ships snapshot enumeration alongside the second reference "
            "backend."
        )

    # ────────────────────────────────────────────────────────────
    # Capabilities

    def capabilities(self) -> ProfileCapabilities:
        return ProfileCapabilities(
            supports_save=True,
            supports_clone=True,
            # Snapshot trio deferred to PR 3 (Decision 3).
            supports_snapshot=False,
            # Hot-reload reserved for future Protocol expansion.
            supports_subscribe=False,
            durable=True,
        )


# ────────────────────────────────────────────────────────────────────
# Module-level helpers


def _unlink_if_exists(path: Path) -> None:
    """Delete a file if present; tolerate the file not existing.

    Used by ``save_profile`` to ensure round-trip semantics: when a
    profile field is empty/None, the corresponding on-disk file is
    removed so a subsequent load doesn't see stale content from before
    the save.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _copy_dir_tree(source: Path, target: Path) -> None:
    """Recursively copy ``source`` directory tree to ``target``.

    Uses Python's ``shutil.copytree`` semantics — preserves directory
    structure and file content. Used by ``clone`` to copy the skills
    directory after the profile itself has been written.
    """
    import shutil

    shutil.copytree(source, target, dirs_exist_ok=False)

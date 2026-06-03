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

import json
import logging
import re
import secrets
import shutil
import threading
from dataclasses import replace
from datetime import datetime
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
    PersonaOwnershipConflict,
    SkillFileTraversal,
    SnapshotNotFound,
)
from ..goal import parse_agent_mode_text
from ..judges_md import load_judges_config
from ..mcp import parse_mcp_md_text
from ..persona_link_md import parse_persona_link_md
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


_logger = logging.getLogger(__name__)


# Hidden directory prefix — ``list_agents()`` skips entries starting with
# this so backend-internal storage (``.snapshots/``, future ``.tmp/``)
# doesn't surface as agents.
_HIDDEN_PREFIX = "."

# Sentinels that distinguish an agent directory from a non-agent directory.
# Either ``persona/IDENTITY.md`` (legacy three-file layout) OR
# ``persona.link.md`` (shared-persona reference, #62 PR 2, D-PP-1) is enough.
# Mirrors ``doctor.check_vault``'s requirement at ``doctor.py:386`` for the
# legacy case; the shared-persona case is the new D2a-locked layout.
_IDENTITY_RELATIVE = Path("persona") / "IDENTITY.md"
_SOUL_RELATIVE = Path("persona") / "SOUL.md"
_USER_RELATIVE = Path("persona") / "USER.md"
_PERSONA_LINK_RELATIVE = Path("persona.link.md")


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
        # Per-pair dedup for D-PP-13 migration-window restore event.
        # Mirrors the save-side ``_warned_drop_agents`` set in sqlite.py
        # (D-PP-8). Keyed on ``(agent_id, snapshot_id)`` tuples so the
        # event fires at most once per (agent, snapshot) pair per process.
        #
        # The lock serializes the check+add so two threads concurrently
        # calling restore() with the same (agent_id, snapshot_id) emit
        # exactly one warning instead of two. Only the membership mutation
        # is inside the lock; _logger.warning() is thread-safe and called
        # outside to keep the critical section short.
        #
        # Note: the save-side D-PP-8 dedup (_warned_drop_agents) ships in
        # PR 2 with a different shape and has NOT been retrofitted here --
        # that asymmetry is intentional and documented in follow-up #291.
        self._warned_restore_drop: set[tuple[str, str]] = set()
        self._warned_restore_drop_lock = threading.Lock()

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
        attack shape; the ``.resolve() + .relative_to(scope_root)``
        check below is the load-bearing security boundary.

        **Cascade support (PR 2 of #63):** ``agent_id`` MAY contain
        forward slashes for cascade-layout agents whose identity is a
        multi-segment relative path under ``scope_root`` (e.g.,
        ``"muse/projects/the-unfinished/agents/writer"`` for the
        cascade-shaped layout described in spec/06). The slash refusal
        in the original PR 1 draft was overly restrictive for cascade
        layouts; the ``relative_to`` check is what actually catches
        traversal after path resolution.

        Still refused (security):

        - Empty string — no agent
        - Leading ``.`` — hidden-directory traversal (e.g., ``.hidden``,
          ``.snapshots``)
        - Backslash ``\\`` — Windows-shape path traversal attack
        - ``..`` anywhere in the path — explicit parent-dir token,
          refused before resolution so the operator gets an actionable
          error message instead of an opaque "resolves outside"
        - Final resolved path outside ``scope_root`` (relative_to check
          is the security boundary)
        """
        if not agent_id:
            raise ValueError("agent_id must not be empty")
        if "\\" in agent_id:
            raise ValueError(
                f"agent_id {agent_id!r} contains a backslash — "
                f"filesystem backend rejects Windows-shape path tokens"
            )
        if agent_id.startswith("."):
            raise ValueError(
                f"agent_id {agent_id!r} starts with '.' — refused to "
                f"prevent hidden-directory traversal"
            )
        # Reject ``..`` anywhere (including inside multi-segment cascade
        # paths) so operator typos surface immediately rather than
        # bouncing off the relative_to boundary downstream.
        if ".." in agent_id.split("/"):
            raise ValueError(
                f"agent_id {agent_id!r} contains '..' segment — path traversal refused"
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

    def _persona_link_path(self, agent_root: Path) -> Path:
        return agent_root / _PERSONA_LINK_RELATIVE

    def _is_agent_dir(self, agent_root: Path) -> bool:
        """Return True when ``agent_root`` has either ownership sentinel.

        The framework treats a directory as "an agent" when EITHER of:
        - ``persona/IDENTITY.md`` (legacy three-file layout)
        - ``persona.link.md`` (shared-persona reference, D2a)

        is present. Used by ``load_profile``, ``list_agents``, and
        ``exists`` so the sentinel choice stays uniform across the three
        sites (D-PP-1).

        Operates on the INSTANCE directory only — role-layer persona
        files in a cascade layout do NOT mark a child as an agent on
        their own; the operator must place either sentinel at the
        instance level (D-PP-6).
        """
        return (agent_root / _IDENTITY_RELATIVE).is_file() or (
            agent_root / _PERSONA_LINK_RELATIVE
        ).is_file()

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
        persona_link_path = self._persona_link_path(agent_root)
        identity_present = identity_path.is_file()
        link_present = persona_link_path.is_file()

        # D-PP-1: sentinel admits either layout. D2a: mutual exclusion —
        # both files present is operator error and the framework refuses
        # to guess which one wins.
        if not (identity_present or link_present):
            raise AgentProfileNotFound(
                f"agent {agent_id!r} not found at {agent_root}: "
                f"neither persona/IDENTITY.md nor persona.link.md exists"
            )
        if identity_present and link_present:
            raise PersonaOwnershipConflict(
                f"agent {agent_id!r} has both persona/IDENTITY.md and "
                f"persona.link.md at {agent_root}. Remove one — the two "
                f"are mutually exclusive (D2a). Keep persona.link.md to "
                f"use a shared PersonaBackend record; keep "
                f"persona/IDENTITY.md to use the legacy three-file "
                f"layout."
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
        # judges_md_raw represents ONLY the instance-layer file. When a
        # cascaded agent inherits the floor without authoring its own
        # judges.md, judges_md_raw is None — the structured
        # judges_config still carries the merged effective config via
        # load_judges_config below.
        #
        # The pre-Step-11 draft populated judges_md_raw from the floor
        # text when the instance lacked its own file ("mirror what the
        # runtime composes"). Step 11 adversarial finding P1#1 caught
        # the resulting GHOST-INSTANCE bug: save_profile would then
        # write the floor text to instance/judges.md, materializing a
        # shadow file the operator never authored. From that point
        # forward, every floor change is silently ignored by this
        # agent because the instance file shadows it — exactly the
        # opposite of the cascade carve-out (Decision 5) the design
        # was supposed to preserve. Fix: judges_md_raw is None when
        # the instance file is absent, even if a floor exists.
        judges_md_path = agent_root / "judges.md"
        if judges_md_path.is_file():
            judges_md_raw: str | None = judges_md_path.read_text(encoding="utf-8")
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

        # ── mcp.md — raw text + parsed servers from one read ──
        # Read mcp.md ONCE — both ``mcp_md_raw`` and ``mcp_servers``
        # consume the same bytes. Step 9.1 perf finding F-D flagged
        # the original double-read (``read_text`` + ``parse_mcp_md``
        # internally re-reading). Using ``parse_mcp_md_text`` on the
        # already-loaded raw eliminates one syscall on every load.
        mcp_md_path = agent_root / "mcp.md"
        mcp_md_raw = (
            mcp_md_path.read_text(encoding="utf-8") if mcp_md_path.is_file() else ""
        )
        # NARROW catch: only ``MCPServerConnectFailed`` — the env-var-
        # resolution failure shape parse_mcp_md_text raises. The raw
        # text on the profile is preserved so the operator can write
        # back without losing $VAR references.
        # ``PathTraversalError`` (mcp.md server arg escaping read_paths)
        # is a security finding and MUST propagate — silently returning
        # ``mcp_servers = []`` would mask malicious server declarations
        # at load time. Pre-#63-PR-1-review-pass this was ``except
        # Exception`` which swallowed both — Step 9 pre-landing review
        # finding F-3.
        # IMPORTANT: this caller MUST keep the default `resolve_env=True` because
        # callers consuming `AgentProfile.mcp_servers` expect resolved values. Do
        # NOT pass `resolve_env=False` here -- the AgentProfile snapshot semantic
        # (spec/24 D1) requires resolved env vars.
        if mcp_md_raw:
            try:
                mcp_servers = parse_mcp_md_text(
                    mcp_md_raw,
                    mcp_md_path=mcp_md_path,
                    read_paths=tool_config.get("read_paths"),
                )
            except MCPServerConnectFailed:
                mcp_servers = []
        else:
            mcp_servers = []
        # Sort lexicographically by name (locked decision Q1 from PR 2 prep).
        # Aligns this path with FilesystemMCPServerRegistryBackend.list_mcp_servers()
        # which already sorts (mcp_registry/filesystem.py). spec/36 MUST 5
        # applies to all backends consistently; the pre-#201 declaration order
        # was an implementation detail of parse_mcp_md_text, not a contract.
        mcp_servers = sorted(mcp_servers, key=lambda s: s.name)

        # ── persona/IDENTITY.md, SOUL.md, USER.md — raw text ──
        # When persona.link.md is present (link_present is True), the
        # agent's persona is externally owned by a PersonaBackend.
        # ``load_profile`` returns empty persona fields as a placeholder;
        # ``AtomicAgent.__init__`` repopulates them via the bootstrap
        # sequence at D-PP-4 (calling ``persona_backend.load_persona``
        # for the source of truth and re-deriving agent_mode). Operators
        # reading ``self._profile.persona_identity`` after agent
        # construction see the resolved text; operators reading the
        # AgentProfile returned by ``load_profile`` directly (no framework
        # bootstrap layer) see empty strings and MUST consult
        # ``external_persona_ref`` to drive their own resolution.
        if link_present:
            persona_identity = ""
            persona_soul = ""
            persona_user = ""
        else:
            # Read IDENTITY.md ONCE — used both for persona_identity and
            # to derive agent_mode (Decision 6). Step 9.1 perf finding
            # F-C flagged the original double-read.
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

        # ── agent_mode — derived from in-memory persona_identity (Decision 6) ──
        # When externally owned (persona_identity == ""), this defaults
        # to "reactive" (parse_agent_mode_text's default for empty input).
        # ``AtomicAgent.__init__`` re-derives after PersonaBackend
        # repopulation (D-PP-4) before any consumer reads the mode.
        agent_mode = parse_agent_mode_text(persona_identity)

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
            mcp_servers_resolved=[],  # populated by framework integration layer in agent.py
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

        # ── Persona — instance-layer, gated on ownership (D6) ──
        # When ``persona.link.md`` is present, the agent's persona is
        # owned by a PersonaBackend; ``save_profile`` MUST NOT write
        # ``persona/IDENTITY|SOUL|USER.md`` (the inline persona-text
        # fields on the AgentProfile are denormalized snapshots
        # populated by the framework's bootstrap path, not authoritative
        # state). Silent skip mirrors spec/24 Decision 6's ``agent_mode``
        # ignore-on-save pattern.
        if (agent_root / _PERSONA_LINK_RELATIVE).is_file():
            # Externally owned — skip persona writes entirely. Operators
            # editing the persona must do so via the PersonaBackend
            # (e.g., ``atomic-agents persona <subcommand>`` once PR 3
            # ships the CLI).
            # Clean up legacy orphan files so operators editing them
            # directly don't see ghost effects (P2-A round 2).
            _unlink_if_exists(agent_root / _SOUL_RELATIVE)
            _unlink_if_exists(agent_root / _USER_RELATIVE)
        else:
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
        """Return subdirs of ``scope_root`` with the agent sentinel, lex order.

        Sentinel: either ``persona/IDENTITY.md`` (legacy layout) OR
        ``persona.link.md`` (shared-persona reference, D-PP-1) is enough.
        Hidden directories (names starting with ``.``) are skipped so
        ``.snapshots/`` and friends don't surface as agents.
        """
        if not self._scope_root.is_dir():
            return []
        agents: list[str] = []
        for entry in sorted(self._scope_root.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name.startswith(_HIDDEN_PREFIX):
                # Skip hidden dirs (.snapshots, .tmp, .git, etc.).
                continue
            if self._is_agent_dir(entry):
                agents.append(entry.name)
        return agents

    def exists(self, agent_id: str) -> bool:
        """True when the agent dir has the sentinel (IDENTITY.md or link.md).

        Uses the same predicate as ``list_agents`` and ``load_profile``
        (D-PP-1).
        """
        try:
            agent_root = self._agent_root(agent_id)
        except ValueError:
            return False
        return self._is_agent_dir(agent_root)

    # ────────────────────────────────────────────────────────────
    # Persona ownership composition (#62 PR 2 — D-PP-3 + D-PP-7)

    def external_persona_ref(self, agent_id: str) -> str | None:
        """Return the agent's persona_id when externally owned, else None.

        Reads ``<agent_root>/persona.link.md`` once and returns the
        ``persona_id`` field. Returns None when the file is absent
        (internally-owned agent, legacy three-file layout).

        Raises ``AgentProfileNotFound`` when the agent itself doesn't
        exist — the distinction "agent missing" vs "agent internally
        owned" is load-bearing for the framework's bootstrap path
        (D-PP-3).

        Raises ``PersonaLinkInvalid`` when ``persona.link.md`` exists
        but its contents are malformed; the parser's error message
        carries the file path and the parse failure.

        Operates on the INSTANCE directory only — role-layer files in a
        cascade layout do NOT participate in ownership (D-PP-6).
        """
        agent_root = self._agent_root(agent_id)
        if not self._is_agent_dir(agent_root):
            raise AgentProfileNotFound(
                f"agent {agent_id!r} not found at {agent_root}: "
                f"neither persona/IDENTITY.md nor persona.link.md exists"
            )
        link_path = self._persona_link_path(agent_root)
        if not link_path.is_file():
            return None
        link = parse_persona_link_md(link_path)
        return link.persona_id

    def set_persona_ownership(self, agent_id: str, persona_id: str | None) -> None:
        """Write or remove ``<agent_root>/persona.link.md`` to mark ownership.

        When ``persona_id`` is non-None: writes a fresh ``persona.link.md``
        via ``_io.atomic_write`` containing the locked YAML-in-code-block
        format (D-ER-4). Raises ``PersonaOwnershipConflict`` if
        ``<agent_root>/persona/IDENTITY.md`` exists — enforces D2a at
        write time so operators cannot create a conflicting state via
        the Protocol surface.

        When ``persona_id`` is None: removes ``persona.link.md`` (no-op
        if already absent). The operator is responsible for creating
        ``persona/IDENTITY.md`` afterwards if they want the agent to
        remain visible to ``list_agents``.

        Raises ``ValueError`` when ``persona_id`` fails the charset
        rule (delegated to ``parse_persona_link_md`` via the written
        file's own validation on next read — but checked here for
        fast-fail UX).
        """
        agent_root = self._agent_root(agent_id)
        if not self._is_agent_dir(agent_root):
            raise AgentProfileNotFound(
                f"agent {agent_id!r} not found at {agent_root}: "
                f"set_persona_ownership requires an existing agent"
            )

        link_path = self._persona_link_path(agent_root)

        if persona_id is None:
            # Restore internal ownership — remove the link file.
            _unlink_if_exists(link_path)
            return

        # Validate charset BEFORE writing (fast fail). Reuses the same
        # rule the parser enforces on read so write-time and read-time
        # validation produce matching error messages.
        from ..persona_link_md import _validate_persona_id

        _validate_persona_id(persona_id, source=f"set_persona_ownership({agent_id!r})")

        # D2a enforcement at write time: refuse to create a conflict when
        # ANY part of the legacy three-file layout (IDENTITY.md, SOUL.md,
        # USER.md) exists.  The three files form one indivisible unit;
        # treating only IDENTITY.md as the sentinel would leave orphan
        # SOUL.md/USER.md files that the operator might edit expecting
        # effect, but reads route through PersonaBackend.
        _conflicting = [
            rel
            for rel in (_IDENTITY_RELATIVE, _SOUL_RELATIVE, _USER_RELATIVE)
            if (agent_root / rel).is_file()
        ]
        if _conflicting:
            conflicting_names = ", ".join(str(p) for p in _conflicting)
            raise PersonaOwnershipConflict(
                f"agent {agent_id!r} at {agent_root} already has "
                f"{conflicting_names} — cannot set persona ownership to "
                f"{persona_id!r} without first removing the legacy "
                f"three-file layout. The two are mutually exclusive (D2a)."
            )

        body = (
            f"# Persona link\n\n```yaml\nkind: shared\npersona_id: {persona_id}\n```\n"
        )
        atomic_write(link_path, body)

    # ────────────────────────────────────────────────────────────
    # Skills — delegate to existing skills.py

    def list_skills(self, agent_id: str) -> list[SkillManifest]:
        """Discover ``<agent_root>/skills/*/SKILL.md`` and return manifests."""
        agent_root = self._agent_root(agent_id)
        if not self._is_agent_dir(agent_root):
            raise AgentProfileNotFound(f"agent {agent_id!r} not found at {agent_root}")
        return discover_skills(agent_root)

    def load_skill_body(self, agent_id: str, skill_name: str) -> str:
        """Read the named skill's SKILL.md body (frontmatter stripped).

        Validates ``skill_name`` against path-traversal — Step 9.1
        security + testing specialist finding F-A. Without this guard,
        an operator-supplied ``skill_name`` containing ``/``, ``\\``,
        or ``..`` could escape the agent's skills directory (the
        existing ``_agent_root`` guard only validates ``agent_id``).
        ``load_skill_body(agent_id, "../../../etc/passwd")`` is the
        attack shape; pre-fix it returned ``FileNotFoundError`` only
        because the constructed path didn't exist, giving no security
        signal and offering no defense against valid cross-agent
        paths like ``../<other-agent>/skills/<name>``.
        """
        _validate_skill_name(skill_name)
        agent_root = self._agent_root(agent_id)
        if not self._is_agent_dir(agent_root):
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
    # Snapshot — JSON-based (#63 PR 3 Decision 3)

    def snapshot(self, agent_id: str, label: str) -> str:
        """Snapshot the agent's current profile state. Returns ``snapshot_id``.

        Serializes ``self.load_profile(agent_id).to_dict()`` as JSON to
        ``<scope_root>/.snapshots/<agent_id>/<snapshot_id>/profile.json``
        + ``metadata.json``. Atomic via ``_io.atomic_write``.

        **Why JSON, not directory copy** (Decision 3 of #63 PR 3):
        ``shutil.copytree`` is not atomic at the agent level — a crash
        mid-copy leaves the agent partially snapshotted. The JSON shape
        round-trips through ``AgentProfile.to_dict / from_dict`` and
        ``restore`` writes the profile back via ``save_profile`` which
        uses the same per-file atomic_write discipline as a fresh save.
        Skills are NOT snapshotted — they aren't in ``AgentProfile``
        (Decision 2 of PR 1), and they require their own ``save_skill``
        Protocol surface that PR 3 explicitly does not add.

        Cascade carve-out: for cascaded agents, the snapshot captures
        the post-merge ``AgentProfile`` view (tools_md_raw is the
        cascade-resolved text; judges_md_raw is instance-only). Restore
        writes only the instance layer (per Decision 5 of PR 1); the
        role + project layers are read-only.
        """
        # Reuse load_profile — raises AgentProfileNotFound if missing.
        profile = self.load_profile(agent_id)

        # 6 hex (24 bits) had ~52% collision probability per second at
        # 4K snapshots/sec — fleet-scale concern flagged in Step 11
        # adversarial F-8. 12 hex (48 bits) brings same-second collision
        # at 4K/sec down to ~6e-8.
        snapshot_id = (
            f"snap_"
            f"{datetime.now().astimezone().strftime('%Y-%m-%dT%H%M%S')}_"
            f"{secrets.token_hex(6)}"
        )
        created_at = datetime.now().astimezone().isoformat()

        agent_root = self._agent_root(agent_id)
        snapshots_dir = self._scope_root / ".snapshots" / agent_id / snapshot_id
        snapshots_dir.mkdir(parents=True, exist_ok=False)

        # D3 snapshot composition (#62 PR 3): when the agent's persona is
        # externally owned, drop persona fields from the snapshot blob.
        # PersonaBackend owns the persona history; AgentProfile snapshots
        # become "config snapshots" that carry only the non-persona fields.
        # Internally-owned agents (legacy three-file layout) keep persona
        # fields in the snapshot — their persona IS the AgentProfile.
        snap_profile = profile
        if self.external_persona_ref(agent_id) is not None:
            snap_profile = replace(
                profile,
                persona_identity="",
                persona_soul="",
                persona_user="",
            )

        # Atomic per-file writes via _io.atomic_write. The directory
        # is created above; the metadata + profile files are written
        # individually with fsync+rename atomicity.
        # default=str — AgentProfile.tool_config["read_paths"] holds
        # PosixPath objects from the parser; from_dict re-derives the
        # structured forms from raw text on restore, so stringified
        # paths round-trip losslessly via the parser.
        profile_blob = json.dumps(snap_profile.to_dict(), indent=2, default=str)
        atomic_write(snapshots_dir / "profile.json", profile_blob)
        metadata = {
            "snapshot_id": snapshot_id,
            "label": label,
            "created_at": created_at,
            "agent_id": agent_id,
        }
        atomic_write(snapshots_dir / "metadata.json", json.dumps(metadata, indent=2))
        # Confirm agent_root resolves (defensive — load_profile already
        # checked this; this catches a race between load_profile + write).
        if not agent_root.exists():
            # Roll back the just-created snapshot dir on race.
            shutil.rmtree(snapshots_dir, ignore_errors=True)
            raise AgentProfileNotFound(
                f"agent {agent_id!r} disappeared between load_profile "
                f"and snapshot write — snapshot rolled back"
            )

        return snapshot_id

    def restore(self, agent_id: str, snapshot_id: str) -> None:
        """Restore the agent's profile to a prior snapshot.

        Loads the snapshot's ``profile.json`` + ``metadata.json``,
        validates cross-agent isolation (snapshot's agent_id must match
        ``agent_id``), then calls ``self.save_profile(agent_id,
        restored_profile)`` — atomic via the existing per-file
        atomic_write discipline.

        **Security checks (path-scoping + cross-agent isolation):**

        1. ``snapshot_id`` validated against the
           ``^snap_[\\w\\-T:]+$``-shaped pattern via ``_validate_snapshot_id``.
        2. Snapshot directory path is resolved + ``relative_to`` checked
           under ``<scope_root>/.snapshots/<agent_id>/`` — refuses
           paths that escape via symlinks / `..` even if the validator
           missed them.
        3. ``metadata.agent_id`` MUST equal ``agent_id`` — defensive
           double-check; ``relative_to`` already enforces this at the
           path level, but operators editing metadata to spoof an
           agent_id would otherwise pass the path check.
        4. ``agent_id`` is validated against ``_agent_root`` BEFORE
           any disk access — refuses ``"../../other"``-shape inputs at
           the API boundary so an operator-controlled agent_id cannot
           read metadata.json from outside ``scope_root``. The check
           is structurally identical to ``load_profile`` /
           ``save_profile``; PR 3 Step 11 adversarial review caught
           that the snapshot trio's read-side methods skipped it.
        """
        # Step 11 adversarial F-3: refuse path-traversal agent_id at
        # the API boundary. Without this, list_snapshots/restore would
        # build snapshots_root via raw path concat and the resolve()
        # check below only protects the snapshot_id segment, not the
        # agent_id segment.
        self._agent_root(agent_id)
        _validate_snapshot_id(snapshot_id)

        snapshots_root = self._scope_root / ".snapshots" / agent_id
        snapshot_dir = snapshots_root / snapshot_id

        # Path-scope check: resolved snapshot_dir MUST be under
        # snapshots_root. Catches symlink escapes that the snapshot_id
        # validator can't see.
        try:
            snapshot_dir.resolve().relative_to(snapshots_root.resolve())
        except (ValueError, OSError) as exc:
            raise SnapshotNotFound(
                f"snapshot {snapshot_id!r} for agent {agent_id!r} "
                f"resolves outside snapshots root"
            ) from exc

        metadata_path = snapshot_dir / "metadata.json"
        profile_path = snapshot_dir / "profile.json"
        if not metadata_path.is_file() or not profile_path.is_file():
            raise SnapshotNotFound(
                f"snapshot {snapshot_id!r} not found for agent "
                f"{agent_id!r} at {snapshot_dir}"
            )

        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise SnapshotNotFound(
                f"snapshot {snapshot_id!r} metadata unreadable: {exc}"
            ) from exc

        # Cross-agent isolation — defensive double-check on top of the
        # path-scope check above. If the metadata claims a different
        # agent than the directory it lives in, refuse.
        if metadata.get("agent_id") != agent_id:
            raise SnapshotNotFound(
                f"snapshot {snapshot_id!r} metadata agent_id "
                f"{metadata.get('agent_id')!r} does not match requested "
                f"agent {agent_id!r}"
            )

        try:
            profile_dict = json.loads(profile_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise SnapshotNotFound(
                f"snapshot {snapshot_id!r} profile unreadable: {exc}"
            ) from exc

        # D-PP-13 migration-window event: snapshot was taken before the
        # agent's persona was migrated to PersonaBackend, so the snapshot
        # blob carries non-empty persona fields. Detect + emit once, then
        # drop the fields so save_profile doesn't re-write them.
        _PERSONA_FIELDS = ["persona_identity", "persona_soul", "persona_user"]
        snap_has_persona = any(profile_dict.get(f) for f in _PERSONA_FIELDS)
        if snap_has_persona and self.external_persona_ref(agent_id) is not None:
            pair = (agent_id, snapshot_id)
            # Lock only the check+add so two concurrent restore() calls on
            # the same (agent_id, snapshot_id) emit exactly one warning.
            # _logger.warning is thread-safe; it runs outside the lock to
            # keep the critical section short.
            with self._warned_restore_drop_lock:
                if pair not in self._warned_restore_drop:
                    emit_event = True
                    self._warned_restore_drop.add(pair)
                else:
                    emit_event = False
            if emit_event:
                _logger.warning(
                    "agent_profile_restore_dropped_persona_fields "
                    "agent_id=%s snapshot_id=%s dropped_fields=%s",
                    agent_id,
                    snapshot_id,
                    _PERSONA_FIELDS,
                )
            for field in _PERSONA_FIELDS:
                profile_dict[field] = ""

        # Reconstruct AgentProfile from the JSON dict + write via the
        # existing atomic save path.
        restored_profile = AgentProfile.from_dict(profile_dict)
        self.save_profile(agent_id, restored_profile)

    def list_snapshots(self, agent_id: str) -> list[ProfileSnapshot]:
        """Return chronological-ordered snapshots for ``agent_id``.

        Enumerates ``<scope_root>/.snapshots/<agent_id>/`` and reads
        each subdirectory's ``metadata.json``. Empty list when the
        snapshots dir is absent (no snapshots ever taken for this
        agent). Snapshots with unreadable / missing metadata are
        silently skipped — they're effectively dead.

        ``agent_id`` is validated against ``_agent_root`` BEFORE any
        disk access — refuses ``"../../other"``-shape inputs so an
        operator-controlled agent_id cannot enumerate metadata.json
        from outside ``scope_root`` (Step 11 adversarial F-3).
        """
        self._agent_root(agent_id)
        snapshots_root = self._scope_root / ".snapshots" / agent_id
        if not snapshots_root.is_dir():
            return []

        results: list[ProfileSnapshot] = []
        for entry in snapshots_root.iterdir():
            if not entry.is_dir():
                continue
            metadata_path = entry / "metadata.json"
            if not metadata_path.is_file():
                continue
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                # Skip corrupt metadata — dead snapshot, operator can
                # rm -rf to clean.
                continue
            results.append(
                ProfileSnapshot(
                    snapshot_id=str(metadata.get("snapshot_id", entry.name)),
                    label=str(metadata.get("label", "")),
                    created_at=str(metadata.get("created_at", "")),
                    agent_id=str(metadata.get("agent_id", agent_id)),
                )
            )
        # Sort by created_at (ISO-8601 lexicographic == chronological
        # for tz-aware timestamps).
        results.sort(key=lambda s: s.created_at)
        return results

    # ────────────────────────────────────────────────────────────
    # Capabilities

    def capabilities(self) -> ProfileCapabilities:
        return ProfileCapabilities(
            supports_save=True,
            supports_clone=True,
            # Snapshot trio implemented in #63 PR 3 — JSON-based
            # (Decision 3 of PR 3): both backends serialize
            # ``AgentProfile.to_dict()`` and write atomically through
            # ``save_profile`` on restore. Plan-subagent caught that
            # the original ``shutil.copytree`` design wasn't atomic.
            supports_snapshot=True,
            # Hot-reload reserved for future Protocol expansion.
            supports_subscribe=False,
            durable=True,
            # Filesystem walks ``<agent>/skills/<name>/SKILL.md`` —
            # full skill support. SQLite backend (#63 PR 3) sets False.
            supports_skills=True,
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

    ``symlinks=True`` preserves symlinks as-is rather than resolving
    them at copy time. Step 9.1 security finding F-G: with the default
    ``symlinks=False``, a malicious source agent whose ``skills/`` dir
    contained a symlink to ``/etc/passwd`` or to another agent's
    secrets would have those target contents copied into the cloned
    agent — a cross-agent contamination / data-exfiltration path.
    Preserving the symlinks keeps them as symlinks in the target;
    they'll still error at read time if the target is unreachable, but
    they don't materialize secrets at copy time.
    """
    shutil.copytree(source, target, dirs_exist_ok=False, symlinks=True)


def _validate_skill_name(skill_name: str) -> None:
    """Reject ``skill_name`` values that could escape the agent's skills dir.

    Step 9.1 security + testing specialist finding F-A. Mirrors the
    ``_agent_root`` validation shape — refuse path separators, leading
    ``.``, empty strings, and parent-dir tokens. Raises
    ``SkillFileTraversal`` (the existing security-tagged exception
    from ``skills.py``) for parity with ``load_skill_referenced_file``'s
    traversal-rejection contract.

    Args:
        skill_name: operator-supplied skill identifier.

    Raises:
        SkillFileTraversal: when the value contains a path separator,
            a parent-dir token, or starts with ``.``.
        ValueError: when the value is empty.
    """
    if not skill_name:
        raise ValueError("skill_name must not be empty")
    if "/" in skill_name or "\\" in skill_name:
        raise SkillFileTraversal(
            f"skill_name {skill_name!r} contains a path separator — "
            f"filesystem backend requires plain directory names"
        )
    if skill_name.startswith("."):
        raise SkillFileTraversal(
            f"skill_name {skill_name!r} starts with '.' — refused to "
            f"prevent hidden-directory traversal"
        )
    if ".." in skill_name:
        raise SkillFileTraversal(
            f"skill_name {skill_name!r} contains a parent-dir token — "
            f"path traversal refused"
        )


# Snapshot IDs are generated by ``snapshot()`` as
# ``snap_<YYYY-MM-DDTHHMMSS+TZ>_<6hex>``. The validator refuses
# operator-supplied IDs that don't match this shape — defensive guard
# against path-traversal attacks via the snapshot_id argument to
# ``restore()``. Allows: digits, letters, hyphen, underscore, colon
# (for tz offset like ``+05:30``), plus. Refuses everything else
# including ``/``, ``\\``, ``..``, NULL bytes, control chars.
_VALID_SNAPSHOT_ID = re.compile(r"^snap_[\w\-T:+]+$")


def _validate_snapshot_id(snapshot_id: str) -> None:
    """Reject ``snapshot_id`` values that don't match the generated shape.

    Belt-and-suspenders against path-traversal — the ``relative_to``
    check in ``restore()`` is the actual security boundary, but
    refusing malformed IDs up front gives a cleaner error message and
    blocks attempts before any filesystem access.

    Raises ``SnapshotNotFound`` (not ValueError) so callers can catch
    a single exception type for "snapshot doesn't exist / can't be
    reached" cases.
    """
    if not snapshot_id:
        raise SnapshotNotFound("snapshot_id must not be empty")
    if not _VALID_SNAPSHOT_ID.match(snapshot_id):
        raise SnapshotNotFound(
            f"snapshot_id {snapshot_id!r} is not a valid snapshot id — "
            f"expected snap_<timestamp>_<hex> shape generated by "
            f"snapshot()"
        )

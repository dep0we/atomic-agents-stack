"""Canonical types for the AgentProfileBackend Protocol (spec/24).

The framework's bootstrap surface — ``AtomicAgent.__init__`` resolving an
agent root, ``_load_config()`` parsing each markdown file, ``_load_persona()``
reading the three persona bodies, ``_load_goal_text()`` reading goal.md —
talks to profile backends only through these canonical types. Each backend
translates between its native primitives (a directory tree on the filesystem,
rows in a SaaS database, refs in a git repo) and the canonical types at its
call boundary.

Scaffolding PR (#63 PR 1): no call site routes through the Protocol yet,
and ``AtomicAgent.__init__`` continues to walk the agent directory directly.
PR 2 wires the bootstrap path; the canonical types exist so PR 2 has a
stable contract to wire against.

Two design decisions that shape the dataclass shape:

1. **Typed shadow + raw text.** Every structured config file (``model.md``,
   ``tools.md``, ``judges.md``, ``roster.md``, ``mcp.md``) ships BOTH on the
   profile: the structured form (``model_config: dict`` etc., for DB-backend
   inspection) AND the raw markdown text (``model_md_raw: str`` etc., for
   filesystem round-trip and audit). The filesystem backend writes raw
   text directly; the structured form is derived on load. The reason this
   is mandatory rather than aesthetic: the existing parsers are lossy in
   three security-sensitive places — ``parse_mcp_md`` resolves ``$VAR``
   env refs at parse time (saving from structured would bake secrets into
   files), ``parse_tools_md`` strips operator comments and tilde-expands
   paths, ``parse_roster_md`` strips comments. Raw-text storage is the
   only honest round-trip.

2. **Frozen, but with mutable nested types.** ``AgentProfile`` is
   ``@dataclass(frozen=True)`` for the same reason ``RunRecord`` is —
   immutable semantics across the framework / backend boundary. The dict
   and list field values are themselves mutable in Python (frozen prevents
   field reassignment, not nested-mutation), which matches the precedent
   set by ``JudgesConfig`` (frozen with mutable ``failure_policy`` dict-
   of-dicts). Use ``dataclasses.replace()`` to derive modified profiles.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from ..exceptions import MCPServerConnectFailed
from ..judges_md import JudgesConfig
from ..mcp import MCPServerSpec


# Canonical agent_mode taxonomy. Backends MUST accept arbitrary strings —
# the closed set is documentation, not enforcement, matching the
# ``primitive`` taxonomy precedent in ``logs/types.py``. The values
# match ``goal.VALID_AGENT_MODES`` so the eventual PR 2 wiring keeps a
# single source of truth.

AGENT_MODE_REACTIVE = "reactive"
AGENT_MODE_GOAL_DRIVEN = "goal-driven"
AGENT_MODE_HYBRID = "hybrid"


@dataclass(frozen=True)
class AgentProfile:
    """The complete identity-layer config for one agent — the unit of
    work for ``load_profile`` and ``save_profile``.

    Required fields cover the agent's identity (name + agent_mode) and
    the persona/goal raw bodies that have no useful structured form.
    Structured fields cover the parsed config files; raw-text shadow
    fields preserve the byte-for-byte source for filesystem round-trip
    and audit (see module docstring §1 — "Typed shadow + raw text").

    The ``to_dict`` / ``from_dict`` round-trip is byte-shape preserving
    for raw-text fields; structured fields preserve via the underlying
    parser's idempotency. PR 2 leans on this when wiring
    ``AtomicAgent.__init__`` to load through the backend.

    Fields:
        name: The agent's identifier — the directory name under
            ``agents_root`` for the filesystem reference impl, the row
            primary key for a database backend.
        agent_mode: One of the canonical ``AGENT_MODE_*`` constants
            (``"reactive" | "goal-driven" | "hybrid"``). Documented-
            derived: filesystem backends parse this from
            ``persona_identity`` via ``goal.parse_agent_mode()`` at
            ``load_profile()`` and ignore it on ``save_profile()``
            (the source of truth is ``persona_identity``). Database
            backends MAY persist it as an indexable column for
            registry queries; the column MUST be re-derived from
            ``persona_identity`` on update to avoid divergence.
        model_config: Structured form parsed via
            ``_model.parse_model_md_text(model_md_raw)``. 12 keys —
            see ``_model.parse_model_md_text`` docstring for the
            full vocabulary.
        tool_config: Structured form parsed via
            ``_tools.parse_tools_md_text(tools_md_raw)``. 5 keys —
            ``read_paths``, ``write_paths``, ``read_only_paths``,
            ``external_apis``, ``hard_nos`` — each a list.
        tool_classifications: Structured form parsed via
            ``_tools.parse_tool_classifications_text(tools_md_raw)``.
            Maps tool name to one of the four ActionClass values
            (``read_only | reversible_write | external_side_effect |
            high_risk``). Empty dict when no ``## Tool classification``
            section is present.
        judges_config: ``JudgesConfig`` dataclass parsed via
            ``judges_md.parse_judges_md_text(judges_md_raw)``, OR
            ``None`` when the agent has no ``judges.md`` (the
            framework's pre-#112 opt-in default).
        roster: List of agent names parsed from the ``## Delegate to``
            section of ``roster_md_raw``.
        mcp_servers: List of ``MCPServerSpec`` dataclasses parsed via
            ``mcp.parse_mcp_md_text(mcp_md_raw)``. **Note**: the parser
            resolves ``$VAR_NAME`` env refs to their literal values
            at parse time. ``mcp_md_raw`` retains the original ``$VAR``
            references; the structured form has them resolved. Save
            paths MUST write ``mcp_md_raw``, not re-render from
            ``mcp_servers`` — see Decision 1 in spec/24.
        persona_identity: Raw markdown body of
            ``<agent_root>/persona/IDENTITY.md``. Verbatim;
            no structured parse at the profile layer. Empty string
            when the file is absent (rare — typically required).
        persona_soul: Raw markdown body of
            ``<agent_root>/persona/SOUL.md``. Verbatim. Empty when absent.
        persona_user: Raw markdown body of
            ``<agent_root>/persona/USER.md``. Verbatim. Empty when absent.
        goal_text: Raw markdown body of ``<agent_root>/goal.md``.
            Verbatim, with frontmatter intact. Empty string when the
            file is absent (reactive agents typically have no goal.md).
            Note: ``GoalManager`` operates on this same file via its
            own structured ``Goal`` dataclass; the profile layer holds
            the raw text only.
        model_md_raw: Raw markdown text of ``model.md``. Source of
            truth for filesystem write-back. Empty when file is absent.
        tools_md_raw: Raw markdown text of ``tools.md``. Source of
            truth for filesystem write-back. For cascaded agents, this
            is the post-merge text from
            ``_cascade.resolve_tools_md(cascade)`` (role + instance
            override OR instance-only OR role-only). Save paths MUST
            write only the instance layer — see Decision 5 in spec/24.
        judges_md_raw: Raw markdown text of ``judges.md``, or ``None``
            when the agent has no judges.md (kept distinct from empty
            string so the backend can preserve "absent" vs "empty").
        roster_md_raw: Raw markdown text of ``roster.md``. Empty when
            absent.
        mcp_md_raw: Raw markdown text of ``mcp.md``. **Critical**:
            preserves ``$VAR_NAME`` env-var references verbatim;
            saving from this string never bakes resolved secrets into
            the on-disk file.
        mcp_servers_resolved: List of ``MCPServerSpec`` instances
            populated by the framework integration layer in
            ``agent.py:__init__`` via ``dataclasses.replace()`` AFTER
            ``load_profile()`` returns and BEFORE ``_load_config()``
            builds the AgentConfig. The source is
            ``MCPServerRegistryBackend.load_all_mcp_servers()``, making
            this field substrate-agnostic by construction. Backends MUST
            NOT populate this field; they own ``mcp_servers`` only.
            Default is ``[]``.

            This field is always serialized as ``[]`` in ``to_dict()``
            to keep resolved MCP env secrets out of snapshot JSON files
            on disk. It re-populates from the registry backend at next
            agent construction. See spec/36 Decision 9 and spec/24 D1
            addendum (#201 PR 2 of 5).
    """

    # Required identity
    name: str
    agent_mode: str

    # Structured config (for DB-backend query / inspection)
    model_config: dict[str, Any]
    tool_config: dict[str, Any]
    tool_classifications: dict[str, str]
    judges_config: JudgesConfig | None
    roster: list[str]
    mcp_servers: list[MCPServerSpec]

    # Raw markdown bodies (source of truth for persona/goal)
    persona_identity: str
    persona_soul: str
    persona_user: str
    goal_text: str

    # Raw markdown text for structured config files
    # (source of truth for filesystem write-back)
    model_md_raw: str
    tools_md_raw: str
    judges_md_raw: str | None
    roster_md_raw: str
    mcp_md_raw: str

    # Resolved MCP server specs from the registry backend (#201 PR 2 of 5).
    # Populated by the framework integration layer in agent.py:__init__ via
    # dataclasses.replace() AFTER load_profile() returns. Backends do NOT
    # populate this field (layer separation per spec/24 Decision 7).
    # The field is always serialized as [] in to_dict() (locked decision Q2
    # from prep pass) to keep resolved MCP secrets out of snapshot files on
    # disk. It re-populates from the registry backend at next agent
    # construction. spec/36 Decision 9; spec/24 D1 addendum.
    mcp_servers_resolved: list[MCPServerSpec] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict suitable for ``json.dumps`` /
        database column storage.

        Structured nested types (``judges_config``, ``mcp_servers``)
        are converted to plain dicts/lists via ``dataclasses.asdict``-
        equivalent serialization so the result is JSON-safe. Round-
        tripping through ``from_dict`` reconstructs them as the typed
        dataclasses.

        Note: ``judges_config`` is serialized to ``None`` when absent;
        otherwise to a dict that ``from_dict`` can reconstruct via
        ``judges_md.parse_judges_md_text(judges_md_raw)`` (the raw
        text is the source of truth). Database backends storing the
        structured form for query purposes get a dict; the raw text
        round-trip remains the canonical reconstruction path.
        """
        return {
            "name": self.name,
            "agent_mode": self.agent_mode,
            "model_config": dict(self.model_config),
            "tool_config": dict(self.tool_config),
            "tool_classifications": dict(self.tool_classifications),
            "judges_config": _judges_config_to_dict(self.judges_config),
            "roster": list(self.roster),
            "mcp_servers": [_mcp_spec_to_dict(s) for s in self.mcp_servers],
            # Always serialize as [] (locked decision Q2 from PR 2 prep
            # pass). The field is a framework-populated runtime transient;
            # serializing real values would write resolved MCP env secrets
            # into snapshot JSON files on disk, which contradicts spec/24
            # Decision 1's intent. The field re-populates from the
            # registry backend at next agent construction.
            "mcp_servers_resolved": [],
            "persona_identity": self.persona_identity,
            "persona_soul": self.persona_soul,
            "persona_user": self.persona_user,
            "goal_text": self.goal_text,
            "model_md_raw": self.model_md_raw,
            "tools_md_raw": self.tools_md_raw,
            "judges_md_raw": self.judges_md_raw,
            "roster_md_raw": self.roster_md_raw,
            "mcp_md_raw": self.mcp_md_raw,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentProfile":
        """Build an ``AgentProfile`` from a plain dict.

        The canonical reconstruction path is "raw text + parser":
        if ``model_md_raw`` is present, ``model_config`` is reparsed
        from it (defending against drift between the two forms when
        the source dict came from a database backend that stored
        them separately). When raw text is absent, the structured
        form is used verbatim (database-only round-trip).

        Permissive on missing fields — defaults to empty containers
        rather than raising, matching the ``RunRecord.from_dict``
        precedent. PR 2 wires this onto the ``AtomicAgent.__init__``
        bootstrap path; a single missing optional field should not
        abort agent load.
        """
        # Local imports avoid the parser-cycle at module load time —
        # ``_model``, ``_tools``, ``_roster``, ``mcp``, ``judges_md``
        # all import from ``..exceptions`` which is already in scope
        # via the ``JudgesConfig`` import at the top.
        from .._model import parse_model_md_text
        from .._tools import (
            parse_tool_classifications_text,
            parse_tools_md_text,
        )
        from .._roster import parse_roster_md_text
        from ..judges_md import parse_judges_md_text
        from ..mcp import parse_mcp_md_text

        name = str(d.get("name", ""))
        agent_mode = str(d.get("agent_mode", AGENT_MODE_REACTIVE))

        model_md_raw = str(d.get("model_md_raw", ""))
        tools_md_raw = str(d.get("tools_md_raw", ""))
        judges_md_raw_in = d.get("judges_md_raw")
        judges_md_raw = str(judges_md_raw_in) if judges_md_raw_in is not None else None
        roster_md_raw = str(d.get("roster_md_raw", ""))
        mcp_md_raw = str(d.get("mcp_md_raw", ""))

        # Re-derive structured forms from raw text when possible. When
        # raw is empty, fall back to the dict's structured form so a
        # DB-only round-trip works.
        if model_md_raw:
            model_config = parse_model_md_text(model_md_raw)
        else:
            model_config = dict(d.get("model_config") or {})
            if not model_config:
                model_config = parse_model_md_text("")

        if tools_md_raw:
            # agent_root is not available in the DB round-trip context (from_dict
            # receives only the serialised dict, not the filesystem path). Bare-
            # relative paths in tools_md_raw will be resolved against the process
            # CWD and emit a warning. Operators using DB backends should use
            # absolute or ~-prefixed paths in tools.md, or rely on the filesystem
            # backend's load_profile() which does pass agent_root correctly.
            tool_config = parse_tools_md_text(tools_md_raw)
            tool_classifications = parse_tool_classifications_text(tools_md_raw)
        else:
            tool_config = dict(d.get("tool_config") or {})
            if not tool_config:
                tool_config = parse_tools_md_text("")
            tool_classifications = dict(d.get("tool_classifications") or {})

        if judges_md_raw is not None and judges_md_raw.strip():
            judges_config: JudgesConfig | None = parse_judges_md_text(judges_md_raw)
        else:
            # Source dict may carry the structured judges_config (either
            # as a JudgesConfig instance from a direct caller, or as a
            # plain dict from a database backend whose to_dict() output
            # round-tripped through JSONB columns).
            #
            # JudgesConfig instance → use as-is.
            # None → return None (the framework's pre-#112 opt-in default).
            # dict / other → raise loudly. Step 11 adversarial finding
            # P1#2 caught the silent-loss bug here: the pre-fix code
            # treated dict the same as None, so a DB backend that stored
            # judges_config as JSON without preserving judges_md_raw
            # alongside would silently drop the operator's judge policy
            # on round-trip. The right call is to make the failure mode
            # explicit so PR 3's DB backend either ships
            # ``judges_md_raw`` alongside structured columns (per spec/24
            # Decision 1's typed-shadow + raw-text design) OR implements
            # a proper ``JudgesConfig.from_dict`` reconstruction (tracked
            # as a follow-up issue).
            jc_raw = d.get("judges_config")
            if jc_raw is None:
                judges_config = None
            elif isinstance(jc_raw, JudgesConfig):
                judges_config = jc_raw
            else:
                raise TypeError(
                    f"AgentProfile.from_dict received judges_config as "
                    f"{type(jc_raw).__name__} (not JudgesConfig or None). "
                    f"This means the dict shape from to_dict() round-"
                    f"tripped through a backend that stored structured "
                    f"columns without preserving judges_md_raw. "
                    f"Reconstruction from the dict shape is not yet "
                    f"supported. Backends MUST persist judges_md_raw "
                    f"alongside any structured judges_config columns "
                    f"(spec/24 Decision 1). See follow-up issue for "
                    f"future JudgesConfig.from_dict support."
                )

        if roster_md_raw:
            roster = parse_roster_md_text(roster_md_raw)
        else:
            roster = list(d.get("roster") or [])

        if mcp_md_raw:
            # NARROW catch: only ``MCPServerConnectFailed`` (env-var
            # resolution failure shape). Step 9.1 multi-specialist
            # finding F-B — the same security narrowing applied in
            # ``filesystem.py:load_profile`` (F-3 in Step 9 pre-
            # landing review) must apply here. ``PathTraversalError``
            # raised by ``parse_mcp_md_text`` when an mcp.md server
            # arg escapes ``read_paths`` is a security finding and
            # MUST propagate — silently returning the dict's
            # structured form would mask malicious server declarations
            # in a database-round-trip scenario.
            #
            # Note: this code path doesn't pass read_paths to
            # ``parse_mcp_md_text``, so the in-parser path-traversal
            # check is currently skipped. Narrowing the except still
            # matters because (a) future callers may pass read_paths
            # via from_dict, (b) other unexpected exceptions (yaml
            # errors, OSError) should NOT be silently swallowed.
            # IMPORTANT: this caller MUST keep the default `resolve_env=True` because
            # callers consuming `AgentProfile.mcp_servers` expect resolved values. Do
            # NOT pass `resolve_env=False` here -- the AgentProfile snapshot semantic
            # (spec/24 D1) requires resolved env vars.
            try:
                mcp_servers = parse_mcp_md_text(mcp_md_raw)
            except MCPServerConnectFailed:
                # Env-var unresolvable in this process — fall back to
                # the dict's structured form (best-effort). Raw text
                # is preserved for write-back regardless.
                # Use _mcp_spec_from_dict to reconstruct MCPServerSpec
                # instances (fixes the pre-existing latent bug where
                # the fallback path returned raw dicts instead of
                # MCPServerSpec instances).
                mcp_servers = [
                    _mcp_spec_from_dict(s) for s in (d.get("mcp_servers") or [])
                ]
        else:
            mcp_servers = [_mcp_spec_from_dict(s) for s in (d.get("mcp_servers") or [])]

        # mcp_servers_resolved is a runtime transient populated by
        # agent.py:__init__. The to_dict() path always emits [] for
        # security (locked Q2). When deserializing a dict that DOES
        # contain the field (e.g. a future un-clamped snapshot or a
        # direct test dict), reconstruct MCPServerSpec instances via
        # _mcp_spec_from_dict for correctness. In normal operation
        # this will always be an empty list.
        mcp_servers_resolved = [
            _mcp_spec_from_dict(s) for s in (d.get("mcp_servers_resolved") or [])
        ]

        return cls(
            name=name,
            agent_mode=agent_mode,
            model_config=model_config,
            tool_config=tool_config,
            tool_classifications=tool_classifications,
            judges_config=judges_config,
            roster=roster,
            mcp_servers=mcp_servers,
            persona_identity=str(d.get("persona_identity", "")),
            persona_soul=str(d.get("persona_soul", "")),
            persona_user=str(d.get("persona_user", "")),
            goal_text=str(d.get("goal_text", "")),
            model_md_raw=model_md_raw,
            tools_md_raw=tools_md_raw,
            judges_md_raw=judges_md_raw,
            roster_md_raw=roster_md_raw,
            mcp_md_raw=mcp_md_raw,
            mcp_servers_resolved=mcp_servers_resolved,
        )

    def replace(self, **changes: Any) -> "AgentProfile":
        """Return a copy with the specified fields replaced.

        Convenience wrapper around ``dataclasses.replace`` so callers
        don't need to import it separately. Matches the ``RunRecord``
        precedent.
        """
        return replace(self, **changes)


@dataclass(frozen=True)
class ProfileSnapshot:
    """Metadata for one snapshot in a backend's snapshot history.

    Returned by ``list_snapshots(agent_id)``. The ``snapshot_id`` is
    backend-issued by ``snapshot(agent_id, label)``; the format is
    backend-specific (filesystem may use timestamp + slug; database
    may use UUID; git may use commit sha). Operators receive the id
    back from ``snapshot()`` and pass it verbatim to ``restore()``.

    Fields:
        snapshot_id: Backend-issued unique identifier for this snapshot.
        label: Operator-supplied human-readable label (e.g.,
            ``"pre-tone-rewrite"``, ``"baseline-2026-05-15"``).
        created_at: ISO-8601 with timezone — the moment ``snapshot()``
            was called. Backends MUST use ``datetime.now().astimezone()``
            or equivalent to produce tz-aware timestamps.
        agent_id: The agent the snapshot belongs to. Snapshots are
            scoped per-agent; ``restore(agent_id, snapshot_id)`` MUST
            raise ``SnapshotNotFound`` if the snapshot exists but
            belongs to a different agent.
    """

    snapshot_id: str
    label: str
    created_at: str
    agent_id: str


@dataclass(frozen=True)
class ProfileCapabilities:
    """Per-backend capability declaration — see Protocol surface in spec/24.

    Conformance tests assert claim-vs-behavior parity: a backend that
    claims ``supports_clone=True`` MUST implement ``clone()`` without
    raising ``NotImplementedError``; one that claims
    ``supports_snapshot=True`` MUST implement ``snapshot``, ``restore``,
    and ``list_snapshots`` similarly. Honest capabilities let callers
    fail fast against incompatible backends rather than discovering the
    mismatch mid-operation.

    Fields:
        supports_save: True when ``save_profile`` is implemented natively.
            Read-only backends (template libraries, immutable git tag
            mounts) set this False; their ``save_profile`` MAY raise
            ``NotImplementedError``. ``FilesystemAgentProfileBackend``
            is True.
        supports_clone: True when ``clone()`` is implemented. Backends
            advertising False MAY raise ``NotImplementedError`` from
            that method. ``FilesystemAgentProfileBackend`` is True.
        supports_snapshot: True when the snapshot trio (``snapshot``,
            ``restore``, ``list_snapshots``) is implemented natively.
            Backends advertising False MAY raise ``NotImplementedError``
            from those methods. **PR 1 NOTE**:
            ``FilesystemAgentProfileBackend`` declares ``False`` for
            this — snapshot implementation lands in #63 PR 3 along
            with the second reference impl (likely SQLite, where
            snapshots are a row in a snapshots table rather than
            filesystem dir copies).
        supports_subscribe: Reserved for hot-reload notification — a
            future capability that lets a backend push profile-change
            events to a subscribed callback. Both PR 1 reference
            backends will be False; the field is reserved in the
            namespace so future expansion doesn't need a Protocol
            change. ``FilesystemAgentProfileBackend`` is False.
        durable: True when ``save_profile`` reaches a durable medium
            before returning (fsync, replication ack). The reference
            ``FilesystemAgentProfileBackend`` is True (writes go through
            ``_io.atomic_write`` which fsyncs file + parent dir). A
            hypothetical memory-only test backend would be False.
        supports_skills: True when ``list_skills(agent_id)`` and
            ``load_skill_body(agent_id, skill_name)`` return meaningful
            data for the backend's storage shape. ``FilesystemAgentProfileBackend``
            is True (skills are walked from ``<agent>/skills/<name>/SKILL.md``).
            ``SQLiteAgentProfileBackend`` (#63 PR 3) is False — skills
            stay filesystem-only in PR 3; a future Protocol method
            ``save_skill()`` will land when SaaS UI editing requires
            DB-backed skill bodies. Conformance tests for skill
            CONTENT (``test_list_skills_returns_metadata``,
            ``test_load_skill_body_returns_body_without_frontmatter``,
            ``test_clone_copies_skills_directory``) gate on this
            capability; empty-skill tests (``test_list_skills_empty``)
            pass for both backends because non-supporting backends
            return ``[]`` from ``list_skills``.
    """

    supports_save: bool
    supports_clone: bool
    supports_snapshot: bool
    supports_subscribe: bool
    durable: bool
    supports_skills: bool


# ──────────────────────────────────────────────────────────────────
# Internal serialization helpers — keep ``to_dict`` readable.


def _judges_config_to_dict(jc: JudgesConfig | None) -> dict[str, Any] | None:
    """Convert a ``JudgesConfig`` to a JSON-safe dict, preserving every
    operator-relevant field. Returns ``None`` for ``None`` input.

    The shape is documentation-grade — structured backends can store
    these columns directly. The canonical reconstruction path is via
    ``parse_judges_md_text(judges_md_raw)`` since the raw text is
    authoritative; this serialization is for inspection / DB query.
    """
    if jc is None:
        return None
    return {
        "default_backend": jc.default_backend,
        "default_model": jc.default_model,
        "timeout_ms": jc.timeout_ms,
        "judge_captures": jc.judge_captures,
        "read_audit_mode": jc.read_audit_mode,
        "validation": jc.validation,
        "validation_source": jc.validation_source,
        "specialist_axes": list(jc.specialist_axes),
        "tools_md_hash": jc.tools_md_hash,
        "judges_md_hash": jc.judges_md_hash,
        "source_path": jc.source_path,
        # Nested-mutable fields — serialize the structure but leave
        # them as plain dicts/objects. The DB backend stores them as
        # JSON columns; the canonical reconstruction goes through the
        # raw-text re-parse path.
        "class_policy": _class_policy_to_dict(jc.class_policy),
        "failure_policy": {
            cls.value: dict(per_class) for cls, per_class in jc.failure_policy.items()
        },
        "budget": _budget_to_dict(jc.budget),
        "escalation": _escalation_to_dict(jc.escalation),
    }


def _class_policy_to_dict(cp: Any) -> dict[str, Any]:
    """``ClassPolicySnapshot`` → JSON-safe dict. The ``source`` map is
    preserved alongside the four class-policy values."""
    return {
        "read_only": cp.read_only.value,
        "reversible_write": cp.reversible_write.value,
        "external_side_effect": cp.external_side_effect.value,
        "high_risk": cp.high_risk.value,
        "source": dict(cp.source),
    }


def _budget_to_dict(b: Any) -> dict[str, Any]:
    """``BudgetConfig`` → JSON-safe dict."""
    return {
        "daily_usd": b.daily_usd,
        "monthly_usd": b.monthly_usd,
        "per_action_usd": b.per_action_usd,
    }


def _escalation_to_dict(e: Any) -> dict[str, Any]:
    """``EscalationConfig`` → JSON-safe dict.

    ``fallback_on_timeout`` is normalized to a per-class dict in the
    parser; preserve that shape here.
    """
    return {
        "destination": e.destination,
        "auto_decide_after_seconds": e.auto_decide_after_seconds,
        "fallback_on_timeout": dict(e.fallback_on_timeout),
        "resolution_poll_cycle_seconds": e.resolution_poll_cycle_seconds,
    }


def _mcp_spec_to_dict(spec: MCPServerSpec) -> dict[str, Any]:
    """``MCPServerSpec`` -> JSON-safe dict.

    Thin wrapper around ``MCPServerSpec.to_dict()`` (promoted to a public
    class method at PR 4, D-PR4-2). Kept here for backward compatibility
    with any internal callers; delegates to the class method so logic lives
    in one place.

    Note: ``env`` contains parser-resolved values (the ``$VAR_NAME``
    references in the source ``mcp.md`` are resolved to their literal
    env-var values at parse time). Database backends serializing this
    SHOULD treat the ``env`` field as sensitive. The raw text on the
    profile (``mcp_md_raw``) is the safe-to-ship form.
    """
    return spec.to_dict()


def _mcp_spec_from_dict(d: dict) -> MCPServerSpec:
    """Reconstruct ``MCPServerSpec`` from a dict produced by ``_mcp_spec_to_dict``.

    Thin wrapper around ``MCPServerSpec.from_dict()`` (promoted to a public
    class method at PR 4, D-PR4-2). Kept here for backward compatibility
    with any internal callers.

    Used by ``AgentProfile.from_dict`` for both ``mcp_servers`` and
    ``mcp_servers_resolved``. Closes a pre-existing latent bug where the
    ``mcp_servers`` fallback path returned raw dicts instead of
    ``MCPServerSpec`` instances.
    """
    return MCPServerSpec.from_dict(d)

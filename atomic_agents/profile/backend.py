"""AgentProfileBackend Protocol — the contract every profile backend satisfies.

This is one of the open protocols in the protocol-pattern series alongside
MemoryBackend (#57, shipped), LLMBackend (#87, shipped), JudgeBackend (#112,
shipped), LockBackend (#60, shipped), and LogBackend (#61, shipped). Each
Protocol decouples one storage / dispatch axis so the framework's core stays
small and alternate implementations drop in without forking.

Issue #63 frames the urgency: today an Atomic Agent IS a directory.
``AtomicAgent.__init__(name, agents_root, ...)`` derives
``self.agent_root = agents_root / name`` and ``_load_config()`` walks it for
``model.md`` / ``tools.md`` / ``judges.md`` / ``roster.md`` / ``mcp.md`` /
``goal.md`` / ``persona/IDENTITY.md|SOUL.md|USER.md`` / ``skills/*/SKILL.md``.
This is the deepest hardcoded abstraction in the framework and it gates every
SaaS-shape feature on the roadmap — UI-editable agent config, agent registry,
hot reload, clone/snapshot, multi-tenant isolation. The AgentProfileBackend
Protocol seals the layer so operators can plug ``DatabaseAgentProfileBackend``
/ ``GitAgentProfileBackend`` / ``S3AgentProfileBackend`` without touching the
bootstrap path.

Scaffolding PR (#63 PR 1): the Protocol contract + canonical types +
``FilesystemAgentProfileBackend`` reference implementation. PR 2 wires the
backend into ``AtomicAgent.__init__`` (constructor kwarg + env-var default,
``_load_config()`` refactor, ``doctor.check_agent_profile_backend``). PR 3
ships the second reference impl (likely ``SQLiteAgentProfileBackend``) with
parametrized conformance + snapshot implementation. PR 4 locks
``docs/spec/24-agent-profile-backend.md``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..skills import SkillManifest
from .types import AgentProfile, ProfileCapabilities, ProfileSnapshot


@runtime_checkable
class AgentProfileBackend(Protocol):
    """Contract every agent-profile backend implementation must satisfy.

    Implementations MUST NOT subclass this Protocol — it is structural.
    Implementations satisfy it by exposing the methods below with the
    documented behavior. The ``@runtime_checkable`` decorator enables
    ``isinstance(obj, AgentProfileBackend)`` to perform a method-presence
    check (not a signature check — signatures are static-typing's job).

    Scope is bound at backend construction. The framework instantiates one
    ``FilesystemAgentProfileBackend(agents_root)`` per process and uses it
    to resolve all agents under that root. Unlike ``LockBackend`` (spec/21)
    there is no ``scope()`` method; profile backends are scope-flat by
    design — agents are sibling rows / sibling directories under one
    namespace, not nested.

    Capability-gated methods (``clone``, ``snapshot``, ``restore``,
    ``list_snapshots``, and ``save_profile`` for read-only backends) MAY
    raise ``NotImplementedError`` when the corresponding ``capabilities()``
    flag is False. The conformance suite enforces this parity.

    Identity: agent_id strings are operator-supplied, treated as opaque
    by the framework, and MUST be unique within the backend's namespace.
    Filesystem backends use them as directory names; database backends
    use them as primary keys; backends MUST validate that they make safe
    primary keys for their storage (e.g., filesystem rejects names with
    path separators).
    """

    @property
    def backend_id(self) -> str:
        """Stable identifier — e.g., ``"filesystem"``, ``"sqlite"``,
        ``"git"``, ``"s3"``.

        Used by the registry for lookup
        (``get_profile_backend(backend_id)``) and by diagnostic tooling
        that wants to log "which backend stores this agent?". Treat as a
        backwards-compatibility surface — operator deployments may pin
        against these strings in env vars and config.
        """
        ...

    # ────────────────────────────────────────────────────────────
    # Core read / write — always implemented

    def load_profile(self, agent_id: str) -> AgentProfile:
        """Load a complete ``AgentProfile`` for ``agent_id``.

        Semantics:

        * MUST raise ``AgentProfileNotFound`` when ``agent_id`` does not
          exist in the backend. Backends MUST NOT silently return an
          empty profile or a profile populated with defaults — the
          distinction "agent doesn't exist" vs "agent exists but is empty"
          is load-bearing.
        * MUST populate every required field on ``AgentProfile``: ``name``
          mirrors the ``agent_id`` argument; ``agent_mode`` is derived
          from ``persona_identity`` (filesystem backends call
          ``goal.parse_agent_mode``; database backends MAY persist it as
          a column but MUST re-derive on update to avoid drift).
        * MUST populate raw-text shadow fields verbatim from source. The
          filesystem backend reads each markdown file via
          ``path.read_text()`` and assigns to the corresponding
          ``*_md_raw`` field. For cascaded agents, ``tools_md_raw`` is
          the merged text from ``_cascade.resolve_tools_md(cascade)``.
          See spec/24 §"Cascade carve-out" for the full carve-out.
        * MUST populate structured fields from raw text via the existing
          parsers (``_model.parse_model_md_text``,
          ``_tools.parse_tools_md_text``,
          ``_tools.parse_tool_classifications_text``,
          ``judges_md.parse_judges_md_text``,
          ``_roster.parse_roster_md_text``, ``mcp.parse_mcp_md_text``).
          Database backends MAY store the structured form as columns
          for query purposes but the canonical reconstruction is via
          re-parse of the raw text.

        Args:
            agent_id: The agent identifier. Backend-specific validation:
                filesystem backends reject names with path separators
                or starting with ``.`` ; database backends reject names
                that would break their primary-key constraints.

        Returns:
            A fully-populated ``AgentProfile``.

        Raises:
            AgentProfileNotFound: when the agent does not exist.
        """
        ...

    def save_profile(self, agent_id: str, profile: AgentProfile) -> None:
        """Persist ``profile`` for ``agent_id``. Returns when durable.

        Semantics:

        * MUST persist before returning. A crash immediately after
          ``save_profile()`` returns MUST NOT lose the profile.
          Filesystem backends fsync each file; SQL backends ack the
          commit; remote backends wait for server ack.
        * MUST overwrite existing profiles silently. ``clone()`` is the
          create-and-refuse-overwrite primitive; ``save_profile`` is the
          updates-allowed primitive. Operators wanting safe creation
          call ``exists()`` first.
        * For raw-text fields (``persona_identity``, ``persona_soul``,
          ``persona_user``, ``goal_text``, ``model_md_raw``,
          ``tools_md_raw``, ``judges_md_raw``, ``roster_md_raw``,
          ``mcp_md_raw``), the filesystem backend writes the field's
          value verbatim to the corresponding on-disk path via
          ``_io.atomic_write``. **The structured fields
          (``model_config``, ``tool_config``, ``judges_config``,
          ``roster``, ``mcp_servers``, ``tool_classifications``) are
          IGNORED on save by the filesystem backend** — the raw text
          is the source of truth. Database backends MAY persist the
          structured fields as columns for query purposes but the
          canonical write path goes through the raw text.
        * The ``agent_mode`` field is IGNORED on save by the filesystem
          backend (re-derived from ``persona_identity`` at the next
          ``load_profile()``). Database backends MAY update an
          ``agent_mode`` column in the same transaction as
          ``persona_identity`` to keep them in sync.
        * For cascaded agents, save writes ONLY the instance-layer
          files. Project-floor ``judges.md`` and role-layer
          ``tools.md``/``model.md`` are read paths only; the backend
          MUST NOT write them. See spec/24 §"Cascade carve-out".
        * MAY raise ``NotImplementedError`` when
          ``capabilities().supports_save=False`` (read-only backends).

        Args:
            agent_id: target agent identifier.
            profile: the ``AgentProfile`` to persist.
        """
        ...

    def list_agents(self) -> list[str]:
        """Return all agent ids in the backend in lexicographic order.

        Semantics:

        * Returns ids only; full profiles are loaded via ``load_profile``.
        * MUST exclude implementation-internal storage. The filesystem
          reference impl excludes hidden directories (names starting
          with ``.``) so ``.snapshots/``, ``.tmp/`` etc. don't surface
          as agents. Database backends MAY filter by a ``tombstoned``
          column if soft-deletes are supported.
        * MUST exclude entries that fail the "is this an agent?"
          sentinel check. The filesystem reference impl requires
          ``persona/IDENTITY.md`` to be present — a directory with
          neither a persona nor a config file is not an agent and
          MUST NOT appear in the list. Database backends use their
          own analog (e.g., a ``profile_complete`` column).
        * Order is lexicographic for deterministic CLI output. Database
          backends MUST sort at the query layer.

        Returns:
            A new list of agent ids in lexicographic order.
        """
        ...

    def exists(self, agent_id: str) -> bool:
        """Return True when ``agent_id`` would resolve to a valid profile.

        Semantics:

        * MUST be O(1)-ish in a single-agent lookup — backends that
          would otherwise need to walk an entire scope SHOULD use a
          presence check (file exists, row count > 0) rather than a
          full ``load_profile()``.
        * MUST return False (NOT raise) when the agent is missing.
          Operators use ``exists()`` to decide between create-or-update
          flows; raising defeats that pattern.
        * MUST use the same "is this an agent?" sentinel as
          ``list_agents()``. An entry that fails ``list_agents()``'s
          inclusion test MUST NOT pass ``exists()``.
        """
        ...

    # ────────────────────────────────────────────────────────────
    # Persona ownership composition (#62 PR 2 — D-PP-3 + D-PP-7)

    def external_persona_ref(self, agent_id: str) -> str | None:
        """Return the persona_id when the agent's persona is externally owned.

        Returns ``None`` when the agent's persona is internally owned —
        the legacy three-file layout for filesystem backends, or a NULL
        persona_id column for SQLite-shaped backends. Returns the
        persona_id string when the agent is bound to a shared persona
        record via the ``PersonaBackend`` Protocol.

        The framework consults this on every ``AtomicAgent.__init__`` to
        decide whether to read persona fields from the AgentProfile's
        denormalized snapshot (returned by ``load_profile``) or to call
        ``persona_backend.load_persona(persona_id)`` for the source of
        truth. See spec/33 §"Composition with AgentProfileBackend" and
        the locked design doc D-PP-3.

        Semantics:

        * MUST return ``None`` (NOT raise) when the agent is internally
          owned. Operators use this method on the same code path as
          ``load_profile``; raising defeats that pattern.
        * MUST raise ``AgentProfileNotFound`` when the agent itself
          does not exist. The distinction is load-bearing — "agent
          missing" vs "agent internally owned" need different
          downstream handling.
        * The filesystem reference impl reads
          ``<agent_root>/persona.link.md`` once and returns its
          ``persona_id`` field. The SQLite reference impl selects the
          ``persona_id`` column. Both validate the persona_id charset
          at the storage layer; malformed records raise
          ``PersonaLinkInvalid`` (filesystem) or the appropriate
          storage-layer exception.
        * The Protocol method check operates on the INSTANCE directory
          only for filesystem backends (D-PP-6 cascade carve-out).
          Role-layer persona files are read-paths-only and do not
          participate in the ownership trigger.

        Args:
            agent_id: target agent identifier.

        Returns:
            The persona_id string when externally owned, or ``None``
            when internally owned.

        Raises:
            AgentProfileNotFound: when the agent does not exist.
            PersonaLinkInvalid: when the filesystem backend finds a
                malformed ``persona.link.md`` file.
        """
        ...

    def set_persona_ownership(self, agent_id: str, persona_id: str | None) -> None:
        """Mark the agent as externally owned, or restore internal ownership.

        When ``persona_id`` is a non-None string, the agent's persona is
        bound to the named ``PersonaBackend`` record. Subsequent
        ``load_profile`` calls populate persona fields via the framework's
        bootstrap path (see D-PP-4). When ``persona_id`` is ``None``,
        the binding is removed — the agent reverts to internal ownership
        (legacy three-file layout for filesystem backends; NULL column
        for SQLite-shaped backends).

        Semantics:

        * MUST persist before returning (same atomic-write discipline
          as ``save_profile``).
        * Filesystem backends write ``<agent_root>/persona.link.md`` via
          ``_io.atomic_write`` when ``persona_id`` is non-None; remove
          the file when ``None``. Filesystem backends MUST raise
          ``PersonaOwnershipConflict`` when ``persona_id`` is non-None
          AND ``<agent_root>/persona/IDENTITY.md`` already exists —
          enforces D2a at write time so operators cannot create a
          conflicting state by API.
        * SQL-shaped backends ``UPDATE ... SET persona_id = ?``. They
          do NOT raise the ownership conflict on set — the conflict is
          filesystem-only (D-PP-8). The next ``save_profile`` silently
          drops any inline persona text when ``persona_id`` is non-NULL,
          emitting a one-time ``agent_profile_save_dropped_persona_fields``
          log event.
        * MAY raise ``NotImplementedError`` when
          ``capabilities().supports_save=False`` (read-only backends).
        * MUST validate the ``persona_id`` charset at the API boundary
          when non-None — the same Protocol-wide charset
          ``[a-zA-Z0-9_.+@-]+`` enforced by PersonaBackend. Reuse the
          ``parse_persona_link_md`` validation for filesystem backends.

        Args:
            agent_id: target agent identifier.
            persona_id: the persona record id to bind, or ``None`` to
                restore internal ownership.

        Raises:
            AgentProfileNotFound: when the agent does not exist.
            PersonaOwnershipConflict: filesystem backends when both
                ``persona.link.md`` and ``persona/IDENTITY.md`` would
                exist after the write.
            ValueError: ``persona_id`` fails charset validation.
            NotImplementedError: capability not supported.
        """
        ...

    # ────────────────────────────────────────────────────────────
    # Skills — separate Protocol methods (Decision 2)

    def list_skills(self, agent_id: str) -> list[SkillManifest]:
        """Return the agent's skill manifests — metadata only.

        Semantics:

        * Returns the manifests in the same shape ``skills.discover_skills``
          produces today (with ``skill_dir``, ``skill_md_path``, body
          line counts, descriptions, when_to_use). Filesystem backends
          delegate to the existing function; database backends return
          synthetic ``Path`` values pointing at storage-internal
          locations (callers MUST NOT treat the paths as filesystem-
          dereferenceable).
        * Bodies are NOT loaded — this is the metadata-only path that
          matches spec/18's progressive disclosure principle. Use
          ``load_skill_body()`` to fetch a single skill's body.
        * Returns ``[]`` when the agent has no skills directory or no
          skills.

        Args:
            agent_id: target agent.

        Returns:
            List of ``SkillManifest`` instances.

        Raises:
            AgentProfileNotFound: when the agent does not exist.
        """
        ...

    def load_skill_body(self, agent_id: str, skill_name: str) -> str:
        """Return the full markdown body of one skill.

        Semantics:

        * Returns the body text WITHOUT frontmatter (matches
          ``skills.load_skill_body`` precedent — frontmatter is
          metadata, surfaced via ``list_skills`` instead).
        * Raises ``FileNotFoundError`` (filesystem backend) or
          equivalent when the skill name is not in the agent's skill
          set.

        Args:
            agent_id: target agent.
            skill_name: the skill identifier from
                ``SkillManifest.name``.

        Returns:
            The skill's markdown body.

        Raises:
            AgentProfileNotFound: when the agent does not exist.
            FileNotFoundError: when the skill name is unknown.
        """
        ...

    # ────────────────────────────────────────────────────────────
    # Capability-gated — MAY raise NotImplementedError

    def clone(
        self,
        source_id: str,
        target_id: str,
        overrides: dict[str, Any] | None = None,
    ) -> None:
        """Copy the profile from ``source_id`` to ``target_id``, with
        optional field overrides applied at write time.

        Semantics:

        * MUST raise ``AgentProfileNotFound`` when ``source_id`` does
          not exist.
        * MUST raise ``AgentProfileExists`` when ``target_id`` already
          exists. ``clone`` is the safe-create primitive; operators
          wanting to overwrite call ``save_profile`` directly.
        * ``overrides`` keys MUST match ``AgentProfile`` field names.
          Each key's value replaces the corresponding field on the
          source profile before save. Unknown keys raise ``ValueError``.
        * MUST be atomic at the agent level: a crash mid-clone MUST
          NOT leave the target half-populated. Filesystem backends
          may use a temporary directory that's renamed to the final
          target only after all files are written; database backends
          use a single transaction.
        * MUST copy skills as well — the cloned agent has the same
          skills available. Filesystem backends copy the directory
          tree; database backends INSERT into the target's skills
          table.
        * MAY raise ``NotImplementedError`` when
          ``capabilities().supports_clone=False``.

        Args:
            source_id: agent to clone from.
            target_id: new agent id.
            overrides: optional ``{field_name: new_value}`` map.

        Raises:
            AgentProfileNotFound: source missing.
            AgentProfileExists: target already exists.
            ValueError: overrides contains an unknown field name.
            NotImplementedError: capability not supported.
        """
        ...

    def snapshot(self, agent_id: str, label: str) -> str:
        """Create a snapshot of the agent's current profile + skills.

        Semantics:

        * Returns a backend-issued ``snapshot_id`` (string). Backends
          MUST guarantee uniqueness within the agent's snapshot history.
        * MUST be atomic: a crash mid-snapshot MUST NOT leave a
          half-formed snapshot in ``list_snapshots`` output.
        * Snapshots include the full profile state at snapshot time.
          ``restore`` reverses to that state exactly.
        * MAY raise ``NotImplementedError`` when
          ``capabilities().supports_snapshot=False``. PR 1's
          ``FilesystemAgentProfileBackend`` ships with
          ``supports_snapshot=False``; PR 3 implements the trio along
          with the second reference impl.

        Args:
            agent_id: target agent.
            label: operator-supplied human-readable label.

        Returns:
            The new ``snapshot_id`` (passable to ``restore``).

        Raises:
            AgentProfileNotFound: agent missing.
            NotImplementedError: capability not supported.
        """
        ...

    def restore(self, agent_id: str, snapshot_id: str) -> None:
        """Restore the agent's profile + skills to a prior snapshot.

        Semantics:

        * Overwrites the current profile + skills with the snapshot's
          contents. The current state is NOT auto-snapshotted before
          restore — operators wanting a safety snapshot call
          ``snapshot()`` first.
        * MUST raise ``SnapshotNotFound`` when the snapshot id is
          unknown OR belongs to a different agent. The latter rule
          is load-bearing for multi-tenant safety: an operator with
          access to agent A's snapshot ids MUST NOT be able to restore
          them onto agent B.
        * MUST be atomic at the agent level (same shape as ``clone``).
        * MAY raise ``NotImplementedError`` when
          ``capabilities().supports_snapshot=False``.

        Args:
            agent_id: target agent.
            snapshot_id: backend-issued id from ``snapshot()`` or
                ``list_snapshots()``.

        Raises:
            AgentProfileNotFound: agent missing.
            SnapshotNotFound: snapshot id unknown or belongs to a
                different agent.
            NotImplementedError: capability not supported.
        """
        ...

    def list_snapshots(self, agent_id: str) -> list[ProfileSnapshot]:
        """Return all snapshots for ``agent_id`` in chronological order.

        Semantics:

        * Returns ``ProfileSnapshot`` metadata in ``created_at`` order
          (oldest first). The full snapshot bodies are NOT returned;
          ``restore`` is the way to materialize a snapshot.
        * MUST return ``[]`` when the agent has no snapshots (NOT
          raise).
        * MAY raise ``NotImplementedError`` when
          ``capabilities().supports_snapshot=False``.

        Args:
            agent_id: target agent.

        Returns:
            Chronologically-ordered list of snapshot metadata.

        Raises:
            AgentProfileNotFound: agent missing.
            NotImplementedError: capability not supported.
        """
        ...

    # ────────────────────────────────────────────────────────────
    # Capabilities

    def capabilities(self) -> ProfileCapabilities:
        """Backend capability declaration — see ``ProfileCapabilities``.

        Conformance tests assert claim-vs-behavior parity. Honest
        capabilities let callers fail fast against incompatible
        backends rather than discovering the mismatch mid-operation.
        """
        ...

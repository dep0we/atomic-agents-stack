"""Canonical types for the ToolRegistryBackend Protocol (spec/25).

The framework's tool-discovery surface — ``AtomicAgent.__init__`` building
the in-memory ``ToolRegistry`` from operator-supplied descriptors, the
multi-turn loop dispatching the LLM's ``tool_use`` blocks against that
registry — talks to tool-registry backends only through these canonical
types. Each backend translates between its native primitives (a
``<agent>/tools/<name>.{md,py}`` pair on the filesystem, a row in a
SaaS database, a wheel in a PyPI index, a tag in a git repo) and the
canonical types at its call boundary.

Scaffolding PR (#64 PR 1): no call site routes through the Protocol yet,
and ``AtomicAgent.__init__`` continues to accept programmatically-
registered tools via the existing ``tools=ToolRegistry()`` kwarg. PR 2
wires the bootstrap path; the canonical types exist so PR 2 has a
stable contract to wire against.

Three design notes that shape the canonical types:

1. **``ToolRef`` is metadata-only — handlers materialize via ``load_tool``.**
   ``list_tools()`` returns enough to *advertise* a tool (its name +
   description for the system prompt + LLM-routing, classification for
   judge-layer dispatch, optional version for forward compatibility,
   source attribution for diagnostics) without paying the cost of
   importing every handler's Python module at construction time. A
   50-tool agent under filesystem pays no Python import cost on
   ``list_tools``; ``load_tool(name)`` is the lazy-materialization path
   that runs ``importlib.util.spec_from_file_location``. Mirrors the
   ``SkillManifest`` / ``load_skill_body`` shape that spec/18 + spec/24
   established for the skills surface — pay context tokens for
   *capability awareness*, not capability content (CLAUDE.md §6 —
   progressive disclosure by default).

2. **Frozen dataclasses across the framework/backend boundary.** Both
   ``ToolRef`` and ``ToolRegistryCapabilities`` are
   ``@dataclass(frozen=True)`` — backends MUST NOT mutate a returned
   ``ToolRef`` (a backend that added a field to a returned ref would
   silently corrupt the caller's data). Matches the
   ``ProfileCapabilities`` / ``ProfileSnapshot`` / ``LogCapabilities``
   precedent across spec/22 + spec/24.

3. **Version field reserved (Decision 4 of spec/25).** ``ToolRef.version:
   str | None = None`` ships in PR 1's canonical type so the field is
   round-trip safe. The filesystem reference impl always sets
   ``version=None`` (today's on-disk layout has no version semantics);
   future PyPI / git backends will populate it and flip
   ``ToolRegistryCapabilities.supports_versioning=True``. Adding the
   field now (vs retrofitting later) prevents the JSON-shape forward-
   compat break ``mcp_md_raw`` taught the profile arc — same lesson
   applied prophylactically.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class ToolRef:
    """Lightweight metadata for one tool in a backend's catalog.

    Returned by ``ToolRegistryBackend.list_tools()`` — the discovery-
    layer surface that the system-prompt assembler + judge-layer
    dispatcher consume without materializing the handler. The
    framework's existing ``ToolDefinition`` (``atomic_agents.tools``)
    is the **dispatch-layer** type that carries the actual ``handler``
    callable + ``input_schema``; ``ToolRef`` is one level up.

    The two are different types deliberately. ``ToolRef`` describes
    "this tool exists; here's its name, description, classification,
    and where it came from" — the catalog/registry view. ``ToolDefinition``
    describes "this tool is callable right now; here's the handler and
    schema" — the in-memory-dispatch view. ``load_tool(name)`` is the
    bridge: a backend implementation reads enough metadata for ``ToolRef``
    on every ``list_tools`` call, but only pays the import cost when
    ``load_tool`` actually needs the handler.

    Fields:
        name: The tool's identifier as the LLM will call it. Backends
            MUST validate this against path-traversal at the API
            boundary — operator-controlled ``name`` flows into both
            descriptor parsing and (for SQLite/database backends) into
            primary-key columns. The filesystem reference impl refuses
            ``name`` containing ``/``, ``..``, leading ``.``, or
            backslash before any disk access (spec/25 MUST #1).
        description: Human-readable description for the LLM. Pulled
            from the descriptor (``tools/<name>.md`` frontmatter on
            filesystem; descriptor column on SQLite). The judge layer
            also surfaces this in proposal-assembly context.
        classification: Optional per-tool action class (spec/28 + #112
            PR 2a). One of the four ``ActionClass`` enum strings
            (``"read_only"``, ``"reversible_write"``,
            ``"external_side_effect"``, ``"high_risk"``) or ``None``
            when the descriptor doesn't declare one. Empty/missing
            classification flows through the judge layer's safe-default
            (``external_side_effect``) at dispatch — same fallback
            shape as the existing ``ToolDefinition.classification``.
        version: Optional version string. **Reserved** — filesystem
            backend always returns ``None`` because the on-disk layout
            has no version semantics. Future PyPI / git backends set
            this from their native version field (PyPI release
            metadata; git tag/sha) and declare
            ``ToolRegistryCapabilities.supports_versioning=True``.
            ``load_tool(name)`` currently dispatches by name only;
            version-aware dispatch is reserved for a future Protocol
            expansion (Decision 4 of spec/25).
        source: Optional backend-specific origin marker for diagnostics
            and audit. Filesystem sets the descriptor path
            (``/.../<agent>/tools/<name>.md``); PyPI would set the
            package name + version; git would set the commit sha + path.
            Empty string when the backend can't surface a meaningful
            origin (purely structural — does not affect dispatch).
    """

    name: str
    description: str
    classification: str | None = None
    version: str | None = None
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for JSON / database storage.

        Round-trips through ``from_dict``. Future backends storing
        ``ToolRef`` as JSON blobs (SQLite #64 PR 3) consume this output;
        the version field is included even when ``None`` so consumers
        that ALTER TABLE for the future versioning capability don't
        need to backfill.
        """
        return {
            "name": self.name,
            "description": self.description,
            "classification": self.classification,
            "version": self.version,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ToolRef":
        """Build a ``ToolRef`` from a plain dict.

        Permissive on missing fields — defaults to empty string for
        ``source`` and ``None`` for optional ``classification`` /
        ``version``. Matches the ``RunRecord.from_dict`` / ``AgentProfile.from_dict``
        precedent: a single missing optional field should not abort
        backend deserialization.
        """
        return cls(
            name=str(d.get("name", "")),
            description=str(d.get("description", "")),
            classification=d.get("classification"),
            version=d.get("version"),
            source=str(d.get("source", "")),
        )

    def replace(self, **changes: Any) -> "ToolRef":
        """Return a copy with the specified fields replaced.

        Convenience wrapper around ``dataclasses.replace`` so callers
        don't need to import it separately. Matches the
        ``AgentProfile.replace`` precedent.
        """
        return replace(self, **changes)


@dataclass(frozen=True)
class ToolRegistryCapabilities:
    """Per-backend capability declaration — see Protocol surface in spec/25.

    Conformance tests assert claim-vs-behavior parity: a backend that
    claims ``supports_install=True`` MUST implement ``install()`` without
    raising ``NotImplementedError``; one that claims
    ``supports_skills_catalog=True`` MUST implement
    ``list_skills_catalog`` / ``load_skill_catalog_body`` similarly.
    Honest capabilities let callers fail fast against incompatible
    backends rather than discovering the mismatch mid-operation.

    Fields:
        supports_install: True when the backend implements
            ``install(source, version)`` — i.e., it can ingest a tool
            from an operator-supplied source string and persist it for
            future ``list_tools`` calls. ``FilesystemToolRegistryBackend``
            is **False** (Decision 7 of spec/25 — the operator's text
            editor + ``cp`` are the install primitive for filesystem;
            inventing an ``install()`` here would either be a network
            call from a "filesystem" backend or a superfluous ``cp``
            wrapper). Future PyPI / git / HTTP backends flip to True
            in their own arc.
        supports_uninstall: True when ``uninstall(name)`` removes the
            named tool from the backend's catalog.
            ``FilesystemToolRegistryBackend`` is **False** (operator
            removes the file). Tracks ``supports_install`` for most
            backends but the two are separately declared so a read-mostly
            backend (e.g., a remote registry with an admin-only install
            seam) can flip them independently.
        supports_versioning: True when the backend honors
            ``ToolRef.version`` semantically — i.e., the catalog can
            distinguish between multiple versions of the same tool name
            and ``load_tool`` can dispatch by version. **Reserved** —
            filesystem and SQLite (PR 3) both ship as False because
            today's on-disk layout has no version semantics; PR 1
            ships the field so JSON-shape forward-compat holds when a
            future backend flips it (Decision 4 of spec/25).
            ``ToolRef.version`` still round-trips on backends that
            declare False (a future operator pinning ``version="1.2.3"``
            in a descriptor sees the value preserved on the returned
            ``ToolRef`` even though the backend doesn't act on it).
        supports_sandbox_validate: True when ``validate(name)`` runs
            the handler in a sandbox to check runtime behavior, not
            just static import. **Reserved** — both reference backends
            ship as False because the sandbox process model
            (subprocess? container? wasm?) is too unsettled to lock in
            #64 PR 1. ``validate()`` is documented as a STATIC check
            (parse + import + signature) regardless of this capability;
            backends flipping it to True ADD sandbox execution on top
            of the static checks, not in place of them (Decision 6 of
            spec/25).
        supports_skills_catalog: True when the backend also publishes
            an installable **skill catalog** via ``list_skills_catalog()``
            / ``load_skill_catalog_body()``. **Reserved** — both
            reference backends ship False because skill mounting is
            ``AgentProfileBackend``'s domain (spec/24 Decision 2).
            ``AgentProfileBackend.list_skills(agent_id)`` answers "what
            skills does this agent currently mount?"; ToolRegistryBackend's
            future skill surface answers "what skills does this catalog
            publish for installation?" The two are different verbs on
            different layers. Future PyPI / git / company-internal-HTTP
            backends will flip this True in their own arc; spec/24's
            ``save_skill`` reservation stays the per-agent mounting
            primitive (Decision 2 of spec/25).
        durable: True when ``install`` / ``uninstall`` reach a durable
            medium before returning (fsync, replication ack).
            ``FilesystemToolRegistryBackend`` declares True even though
            its capability-gated install/uninstall raise — the catalog
            ITSELF (the on-disk ``<agent>/tools/`` dir) is durable. A
            hypothetical in-memory test backend would be False on both
            axes.
    """

    supports_install: bool
    supports_uninstall: bool
    supports_versioning: bool
    supports_sandbox_validate: bool
    supports_skills_catalog: bool
    durable: bool


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of ``ToolRegistryBackend.validate(name)`` — a static check.

    ``validate()`` is the audit-time call: parses the descriptor,
    attempts the handler import inside a ``try``/``except``, checks
    the handler is callable with the expected signature. **Does NOT
    execute the handler** (Decision 6 of spec/25 — sandboxed execution
    is a separate capability). The result distinguishes between hard
    errors (descriptor unparseable, import failed, signature wrong —
    tool is unusable) and soft warnings (missing classification,
    description shorter than the LLM-routing threshold — tool is
    usable but flagged).

    Mirrors the LLM ``ValidationResult`` shape used by spec/31 +
    ``LLMBackend.validate`` — same field names so operator tooling
    that surfaces validation output stays uniform across backend
    types.

    Fields:
        ok: ``True`` when no errors were collected. Equivalent to
            ``not errors`` — kept as an explicit field so callers
            can pattern-match without re-checking the list.
        errors: List of human-readable error strings. Empty when the
            tool is fully usable. Each entry is a single-line
            actionable diagnostic (e.g., ``"handler module
            <path> failed to import: ModuleNotFoundError: No module
            named 'requests'"``). The diagnostic level for "tool
            unusable" — operators triaging a tool failure read this
            list first.
        warnings: List of human-readable warning strings. Empty when
            the tool is fully usable AND well-formed. Non-empty when
            the tool is usable but has cosmetic / hygiene issues
            (missing classification, missing description, etc.).
            Operators reviewing a fresh catalog read this list to
            decide whether to polish before shipping.
    """

    ok: bool
    errors: list[str]
    warnings: list[str]

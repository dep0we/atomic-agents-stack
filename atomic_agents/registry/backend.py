"""ToolRegistryBackend Protocol — the contract every tool-registry backend satisfies.

This is the seventh open Protocol in the protocol-pattern series alongside
MemoryBackend (#57, shipped), LLMBackend (#87, shipped), JudgeBackend (#112,
shipped), LockBackend (#60, shipped), LogBackend (#61, shipped), and
AgentProfileBackend (#63, shipped). Each Protocol decouples one storage /
dispatch axis so the framework's core stays small and alternate
implementations drop in without forking.

Issue #64 frames the urgency: today an Atomic Agent's tool catalog is a
filesystem walk hardcoded into ``AtomicAgent.__init__`` plus programmatic
``ToolRegistry().register(...)`` calls. This caps the ecosystem at "files
on the box": a plugin marketplace, version-pinned tools, SaaS multi-tenant
tool catalogs, audit/approval flows on install, and sandboxed-execution
shapes all require lifting the discovery seam to a Protocol. The same way
#63 closed the SaaS-shape cliff for the bootstrap path, ``ToolRegistryBackend``
closes the **plugin-ecosystem cliff** for the capability path — PyPI / git
/ company-internal HTTP / SaaS-database tool catalogs become one Protocol
implementation away.

Scaffolding PR (#64 PR 1): the Protocol contract + canonical types +
``FilesystemToolRegistryBackend`` reference implementation + DRAFT spec/25
+ ~60 conformance + filesystem tests. **Zero behavior change** — existing
``tools.py`` / ``skills.py`` / ``agent.py`` code paths untouched; the new
module is registered at import but not consumed. PR 2 wires the backend
into ``AtomicAgent.__init__`` + all four runners + ``doctor.check_tool_registry_backend``.
PR 3 ships the second reference impl (likely ``SQLiteToolRegistryBackend``)
with parametrized conformance + the install/uninstall capability flag
flipped True. PR 4 locks ``docs/spec/25-tool-registry-backend.md``.

The Protocol covers two seams:

1. **Catalog discovery (always implemented).** ``list_tools()`` returns
   ``ToolRef[]`` — lightweight metadata. ``load_tool(name)`` returns a
   ``ToolDefinition`` (the existing dispatch-layer type from
   ``atomic_agents.tools``) carrying the executable handler. The two
   are separated so a 50-tool agent doesn't pay Python import cost on
   every construction — only on first dispatch.

2. **Capability-gated mutation.** ``install(source, version)`` /
   ``uninstall(name)`` flip ``supports_install`` / ``supports_uninstall``
   when the backend has install semantics (SQLite #64 PR 3; future
   PyPI). The filesystem reference impl declares both False — the
   operator's text editor + ``cp`` are the install primitive there.

A reserved future expansion (capability-gated ``list_skills_catalog`` /
``load_skill_catalog_body``) will surface installable skills as a
ToolRegistryBackend concern, distinct from ``AgentProfileBackend.list_skills``
which owns the per-agent **mounted** skill view. The two address
different verbs on different layers — Decision 2 of spec/25 + spec/24's
locked ``save_skill`` reservation.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..tools import ToolDefinition
from .types import ToolRef, ToolRegistryCapabilities, ValidationResult


@runtime_checkable
class ToolRegistryBackend(Protocol):
    """Contract every tool-registry backend implementation must satisfy.

    Implementations MUST NOT subclass this Protocol — it is structural.
    Implementations satisfy it by exposing the methods below with the
    documented behavior. The ``@runtime_checkable`` decorator enables
    ``isinstance(obj, ToolRegistryBackend)`` to perform a method-presence
    check (not a signature check — signatures are static-typing's job).

    Scope is bound at backend construction. ``FilesystemToolRegistryBackend(agent_root)``
    is scoped per-agent — every agent has its own ``<agent>/tools/``
    directory and the backend resolves tool names within that dir.
    ``SQLiteToolRegistryBackend(db_path, agent_scope=...)`` (PR 3) is
    similarly per-agent at the row level (WHERE agent_scope = ?). A
    future ``PyPIToolRegistryBackend`` would be process-shared because
    the catalog itself is shared; the Protocol does NOT prescribe the
    constructor signature — only the methods.

    **Relationship to the in-memory ``ToolRegistry``** (``atomic_agents.tools``):
    ``ToolRegistryBackend`` owns the **discovery layer** — the catalog
    of what tools are available + the materialization path for their
    handlers. ``ToolRegistry`` (the existing dispatch class) stays as
    the LLM-tool-loop dispatcher used during ``call()``:
    ``register()`` / ``unregister()`` / ``execute()`` /
    ``to_anthropic_definitions()``. **The two compose** (Decision 1 of
    spec/25):

    .. code-block:: text

        backend.list_tools() → list[ToolRef]             # discovery
        backend.load_tool(name) → ToolDefinition         # materialization
        agent.tool_registry.register(td)                 # in-memory dispatch (unchanged)

    Replacing the in-memory ``ToolRegistry`` would touch every provider
    format builder, the multi-turn loop's ``tool_registry.execute()``
    call site, and the MCP overlay. Massive blast radius for no benefit.
    The discovery layer is the actual hardcoded-filesystem seam that
    needs lifting; the in-memory registry is the right shape and stays.

    Capability-gated methods (``install``, ``uninstall``,
    ``list_skills_catalog``, ``load_skill_catalog_body``) MAY raise
    ``NotImplementedError`` when the corresponding ``capabilities()``
    flag is False. The conformance suite enforces this parity.

    Identity: tool ``name`` strings are operator-supplied, treated as
    opaque by the framework, and MUST be unique within the backend's
    scope. Filesystem backends use them as filename stems; database
    backends use them as primary keys. Backends MUST validate that
    ``name`` is safe for their storage shape — the filesystem reference
    impl refuses names containing path separators, leading ``.``, or
    parent-dir tokens at the API boundary (spec/25 MUST #1).
    """

    @property
    def backend_id(self) -> str:
        """Stable identifier — e.g., ``"filesystem"``, ``"sqlite"``,
        ``"pypi"``, ``"git"``.

        Used by the registry for lookup
        (``get_tool_registry_backend(backend_id)``) and by diagnostic
        tooling that wants to log "which catalog stores this tool?".
        Treat as a backwards-compatibility surface — operator
        deployments may pin against these strings in env vars and
        config.
        """
        ...

    # ────────────────────────────────────────────────────────────
    # Discovery — always implemented

    def list_tools(self) -> list[ToolRef]:
        """Return every tool the backend can materialize, sorted by name.

        Semantics:

        * Returns ``ToolRef`` instances — metadata only. Handlers are
          NOT loaded; the import cost only fires inside ``load_tool``.
          This mirrors spec/18's progressive disclosure principle
          (metadata in the system prompt; bodies on demand) applied to
          tools.
        * MUST return ``[]`` (NOT raise) when the catalog is empty.
          For the filesystem reference impl, "no ``tools/`` directory"
          and "empty ``tools/`` directory" both produce ``[]`` — they
          are semantically identical.
        * MUST exclude implementation-internal entries. The filesystem
          reference impl excludes hidden files (names starting with
          ``.``) and excludes Python module helpers (names starting
          with ``_`` or named ``__init__.py``).
        * Order is lexicographic by ``name`` for deterministic CLI
          output and test reproducibility. Database backends MUST sort
          at the query layer (``ORDER BY name``).

        Returns:
            A new list of ``ToolRef`` instances in lexicographic order.
        """
        ...

    def load_tool(self, name: str) -> ToolDefinition:
        """Materialize the named tool — return a usable ``ToolDefinition``.

        Semantics:

        * Returns a fully-populated ``ToolDefinition`` (from
          ``atomic_agents.tools``) carrying the executable ``handler``
          callable, the JSON ``input_schema``, the ``description``,
          and the ``classification`` (when present). Callers register
          the returned definition into the in-memory ``ToolRegistry``
          via ``agent.tool_registry.register(td)`` — Decision 1 of
          spec/25's composition pattern.
        * MUST raise ``ToolNotInRegistry`` when ``name`` is absent
          from the catalog. Distinct from
          ``ToolNotRegistered`` (which is raised by the in-memory
          ``ToolRegistry.execute`` for an LLM-emitted tool_use against
          an unknown tool). The two exceptions cover different layers
          and are NOT interchangeable.
        * MUST raise ``ToolDescriptorInvalid`` when the descriptor
          (filesystem ``<name>.md`` frontmatter; SQLite descriptor
          row) cannot be parsed.
        * MUST raise ``ToolHandlerImportFailed`` when the handler
          module cannot be imported (filesystem ``<name>.py`` raises
          on import; SQLite handler source ``exec()`` raises).
        * MUST validate ``name`` against path-traversal at the API
          boundary BEFORE any disk access. The filesystem reference
          impl refuses ``name`` containing ``/``, ``..``, leading
          ``.``, or backslash; SQLite refuses same in the descriptor's
          name column on install. Spec/25 MUST #1.
        * Handlers MUST be re-importable. The filesystem reference
          impl does **not** populate ``sys.modules`` (each call to
          ``load_tool`` produces a fresh module via
          ``importlib.util.spec_from_file_location`` +
          ``exec_module``), so operators with side-effecting top-
          level code in their handler module see the side effect
          re-fire on every load_tool call. Backends MAY opt to cache
          (insert into ``sys.modules`` under a deterministic
          qualname) but MUST NOT cache the returned ``ToolDefinition``
          instance — callers may mutate fields on the definition
          before registering it (operator wrapping a backend-loaded
          handler with logging, for example). PR 2 wires
          ``load_tool`` exactly once per agent construction, so
          re-import overhead lands only on agent re-construction.

        Args:
            name: tool identifier. Treated as an opaque string by the
                Protocol; backends validate against their storage
                shape's path / key rules.

        Returns:
            A ``ToolDefinition`` carrying the handler + schema +
            classification.

        Raises:
            ToolNotInRegistry: ``name`` is not in the catalog.
            ToolDescriptorInvalid: descriptor cannot be parsed.
            ToolHandlerImportFailed: handler module cannot be imported.
            ValueError: ``name`` fails path-traversal validation.
        """
        ...

    def validate(self, name: str) -> ValidationResult:
        """Static check — parse descriptor + import handler + signature check.

        Semantics:

        * **Does NOT execute the handler.** ``validate()`` is the audit-
          time call; the runtime safety story is the judge layer
          (spec/28) + the in-memory ``ToolRegistry.execute()``. Spec/25
          Decision 6 — sandboxed-execution-as-validation is reserved
          for a future ``supports_sandbox_validate=True`` capability.
        * MUST parse the descriptor (or raise via ``errors`` —
          ``ValidationResult.ok=False``).
        * MUST attempt the handler import inside a ``try``/``except``
          (any import failure surfaces in ``errors``, NOT propagates).
        * MUST check the handler is callable. A handler module that
          imports cleanly but doesn't expose a callable named
          ``handler`` is an ``errors`` entry, not a warning — the
          tool is unusable.
        * Cosmetic / hygiene issues (missing ``description``,
          ``classification`` not declared) surface in ``warnings`` —
          tool is usable but flagged.
        * MUST NOT raise on a missing tool. ``validate()`` of a name
          absent from the catalog returns ``ValidationResult(ok=False,
          errors=["tool 'X' not in registry"], warnings=[])`` — the
          caller treats the absence as a validation failure, not an
          exception. This matches the LLM ``validate`` precedent.

        Args:
            name: tool identifier to validate.

        Returns:
            ``ValidationResult`` carrying ``ok`` flag, ``errors`` list,
            ``warnings`` list.
        """
        ...

    # ────────────────────────────────────────────────────────────
    # Capability-gated mutation — MAY raise NotImplementedError

    def install(self, source: str, version: str | None = None) -> ToolRef:
        """Install a tool into the catalog from ``source``.

        Semantics:

        * Returns the freshly-installed ``ToolRef`` so callers can
          confirm the resulting ``name`` + ``classification`` +
          ``version``.
        * MUST be atomic at the tool level: concurrent
          ``install(source=X)`` calls — exactly one wins, the others
          raise (spec/25 MUST #7). The reference SQLite backend uses
          ``INSERT INTO tools ... ON CONFLICT(agent_scope, name) DO
          NOTHING`` returning a row count; zero rows raises
          ``ToolAlreadyInstalled``.
        * ``source`` is backend-specific. SQLite (#64 PR 3) accepts
          ``local://<path>`` (descriptor + handler from a filesystem
          path) and bare filesystem paths. Future PyPI accepts
          ``<package>==<version>``; git accepts ``<remote>#<sha>``.
        * ``version`` is reserved (Decision 4 of spec/25). Backends
          declaring ``supports_versioning=False`` (today: both
          reference impls) MUST accept ``version=None`` and reject
          non-None values with ``ValueError``. Future versioning-
          aware backends honor it.
        * MAY raise ``NotImplementedError`` when
          ``capabilities().supports_install=False``. Filesystem
          reference impl is False — operators install by editing
          ``<agent>/tools/`` directly.

        Args:
            source: backend-specific source string.
            version: optional version pin. Reserved — must be None
                unless the backend declares
                ``supports_versioning=True``.

        Returns:
            The new ``ToolRef``.

        Raises:
            NotImplementedError: capability not supported.
            ValueError: ``source`` malformed, or ``version`` passed
                to a backend without versioning support.
            ToolAlreadyInstalled: a tool with the same name already
                exists in the catalog (spec/25 MUST #7).
        """
        ...

    def uninstall(self, name: str) -> None:
        """Remove ``name`` from the catalog.

        Semantics:

        * MUST be idempotent — uninstalling a name that doesn't exist
          is a no-op (no exception). The conformance suite asserts
          this; matches the in-memory ``ToolRegistry.unregister``
          precedent.
        * MUST validate ``name`` against path-traversal at the API
          boundary (spec/25 MUST #1).
        * MAY raise ``NotImplementedError`` when
          ``capabilities().supports_uninstall=False``.

        Args:
            name: tool identifier to remove.

        Raises:
            NotImplementedError: capability not supported.
            ValueError: ``name`` fails path-traversal validation.
        """
        ...

    # ────────────────────────────────────────────────────────────
    # Reserved — capability-gated skill catalog (spec/25 Decision 2)

    def list_skills_catalog(self) -> list[ToolRef]:
        """Return the **installable skill catalog** for this backend.

        Reserved — both reference backends in PR 1 declare
        ``supports_skills_catalog=False`` and raise
        ``NotImplementedError``. The future implementation will return
        ``ToolRef``-shaped metadata describing skills available for
        installation (distinct from
        ``AgentProfileBackend.list_skills(agent_id)`` which returns
        the agent's **currently mounted** skills).

        Documented here so future operators / contributors searching
        for "list_skills" on this Protocol find the reservation +
        rationale (spec/25 Decision 2), not silence.

        Raises:
            NotImplementedError: capability not supported.
        """
        ...

    def load_skill_catalog_body(self, name: str) -> str:
        """Return the body of a skill from the catalog.

        Reserved — same shape as ``list_skills_catalog`` above. Both
        reference backends raise ``NotImplementedError``.

        Raises:
            NotImplementedError: capability not supported.
        """
        ...

    # ────────────────────────────────────────────────────────────
    # Capabilities

    def capabilities(self) -> ToolRegistryCapabilities:
        """Backend capability declaration — see ``ToolRegistryCapabilities``.

        Conformance tests assert claim-vs-behavior parity. Honest
        capabilities let callers fail fast against incompatible
        backends rather than discovering the mismatch mid-operation.
        """
        ...

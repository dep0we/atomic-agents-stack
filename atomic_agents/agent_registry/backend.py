"""AgentRegistryBackend Protocol — the contract every implementation satisfies.

This is the twenty-second open Protocol in the protocol-pattern series (spec/51).
It provides fleet-level agent enumeration and governance metadata so the dashboard
and operator tooling discover agents without relying on the log/-presence heuristic
(which excludes newly-deployed agents that have never run).

Protocol method surface (5 methods):

    list_agents(*, include_governance=True) -> list[AgentRef]
        Enumerate all framework-recognized agents under agents_root.
        A folder is recognized iff model.md is present AND readable (spec/37:314,
        Implementer-Contract MUST 4). Returns [] when agents_root is absent or
        has no qualifying dirs (MUST 2). MUST be fail-soft (MUST 5): a single
        malformed/symlinked agent MUST NOT abort the full enumeration.
        include_governance=False skips the per-agent governance.md read+parse
        for callers that need only the id list (progressive disclosure,
        Principle #6) — those entries carry has_governance=False, governance=None.

    get_agent(agent_id) -> AgentEntry | None
        Return the full AgentEntry (alias of AgentRef) for agent_id, or None on
        miss (like read_note). TOCTOU-safe: if model.md vanishes between list and
        get, returns None (MUST 7).

    capabilities -> AgentRegistryCapabilities
        Backend capability declaration. NOTE: property, not method call —
        mirrors mcp_registry/backend.py:147 (@property convention, MUST 9).

    register_agent(entry: AgentRef) -> None
        Persist a registry entry. FilesystemAgentRegistryBackend raises
        RegistrationNotSupported (read-only discovery backend, spec/51 MUST 10).

    unregister_agent(agent_id: str) -> None
        Remove a registry entry. FilesystemAgentRegistryBackend raises
        RegistrationNotSupported (same read-only constraint, MUST 10).

NOTE: The canonical verb is ``unregister_agent`` (matching the 19 existing
``unregister_*`` methods in the framework). Do NOT use ``deregister_agent``
anywhere — that form was mentioned in the original issue text but was ruled
incorrect by the maintainer before implementation began.

Import boundary (circular-import safety):
    This module imports ONLY from .types, ..exceptions, and stdlib.
    No imports from ..agent, .._llm, .._costs, ..logs, or any module that
    transitively imports those — so it forms no import cycle with the LLM stack.
    NOTE: importing the package still triggers atomic_agents/__init__.py, which
    eagerly loads the LLM stack; the boundary here is cycle-safety, not lazy-load.

See docs/spec/51-agent-registry-backend.md for the full normative contract.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from .types import AgentEntry, AgentRef, AgentRegistryCapabilities

_logger = logging.getLogger(__name__)


@runtime_checkable
class AgentRegistryBackend(Protocol):
    """Contract every agent registry backend implementation must satisfy.

    Implementations MUST NOT subclass this Protocol — it is structural.
    Implementations satisfy it by exposing the methods below with the
    documented behavior. @runtime_checkable enables isinstance() method-
    presence checks.

    Scope: fleet-level — scoped at agents_root (NOT agent_root). This is
    the same scope as AgentProfileBackend, not the per-agent scope of
    JournalBackend/IdempotencyBackend.

    The backend is STATELESS at the Protocol level — it holds agents_root
    only. All registry state is derived from the filesystem (filesystem
    backend) or stored in a shared database (future Postgres backend).
    """

    @property
    def backend_id(self) -> str:
        """Stable identifier — e.g. 'filesystem'.

        Used by the registry for lookup and by diagnostic tooling. Treat as a
        backward-compatibility surface — operator deployments may pin against
        these strings.
        """
        ...

    def list_agents(self, *, include_governance: bool = True) -> list[AgentRef]:
        """Enumerate all framework-recognized agents under agents_root.

        A folder qualifies as a framework agent iff (spec/51 Implementer
        Contract MUST 4, the spec/37:314 discovery predicate):
        - It is a direct subdirectory of agents_root (not recursive).
        - Its name does NOT begin with '_' or '.' (MUST 3).
        - model.md is present AND readable without IOError (spec/37:314
          verbatim: 'model.md is present AND parse_model_md() returns without
          raising'; parse_model_md() tolerates malformed YAML, so the exclusion
          condition is 'model.md missing OR unreadable — IOError').
        - Its resolved path is contained within agents_root (symlink containment
          guard — part of the MUST 5 fail-soft contract; a symlinked agent
          resolving outside agents_root is skipped, never raised).

        Returns [] (authoritative empty, MUST 2) when:
        - agents_root does not exist
        - agents_root is not a directory
        - No qualifying subdirectory exists
        - A PermissionError prevents enumeration (fail-soft, log WARNING)

        MUST be fail-soft per agent (spec/51 MUST 5):
        - A single agent with a malformed governance.md MUST NOT abort the loop.
        - A single symlinked agent outside agents_root MUST be skipped, not raised.
        - Every per-agent error is logged at WARNING; the loop continues.

        Args:
            include_governance: when True (default), each AgentRef carries the
                parsed governance.md record (has_governance / governance). When
                False, the per-agent governance.md read+parse is SKIPPED and
                every entry carries has_governance=False, governance=None — for
                id-only callers (e.g. dashboard discover_agents) that never read
                governance, this avoids ~N wasted file reads per call
                (progressive disclosure, Principle #6). The discovery predicate
                and sort order are identical for both values.

        Returns:
            list[AgentRef] — one entry per qualifying agent, sorted lexicographically
            by agent id (MUST 6). discovered_at is the ISO-8601 UTC call-time
            timestamp.
        """
        ...

    def get_agent(self, agent_id: str) -> AgentEntry | None:
        """Return the full AgentEntry for agent_id, or None on miss.

        Mirrors read_note's None-on-miss contract. TOCTOU-safe: if model.md
        vanishes between a list_agents() call and this call, returns None
        rather than raising.

        Args:
            agent_id: the agent folder name (string id from AgentRef.id).

        Returns:
            AgentEntry (AgentRef) if the agent exists and model.md is readable
            (None on miss — MUST 7).
            None if the agent folder does not exist, model.md is absent, or
            model.md is unreadable (OSError — TOCTOU-safe).

        Raises:
            PathTraversalError: if agent_id contains a path separator, is
                equal to '.' or '..', or is empty (path traversal guard, MUST 8).
        """
        ...

    @property
    def capabilities(self) -> AgentRegistryCapabilities:
        """Backend capability declaration.

        NOTE: This is a @property, not a method call (MUST 9) — mirrors the
        mcp_registry/backend.py:147 convention. Callers use
        ``backend.capabilities`` (no parentheses).

        Conformance tests assert claim-vs-behavior parity. Honest capabilities
        let callers fail fast against incompatible backends.

        MUST: backend_id, supports_registration, and supports_canonical_export
        must be declared. FilesystemAgentRegistryBackend always declares
        supports_registration=False and supports_canonical_export=False.
        """
        ...

    def register_agent(self, entry: AgentRef) -> None:
        """Persist a registry entry (MUST 10 — raises on read-only backends).

        FilesystemAgentRegistryBackend raises RegistrationNotSupported because
        it is a discovery-only (read-only) backend. A future database-backed
        backend may implement this method.

        Args:
            entry: the AgentRef to register.

        Raises:
            RegistrationNotSupported: when the backend does not support writes.
        """
        ...

    def unregister_agent(self, agent_id: str) -> None:
        """Remove a registry entry (MUST 10 — raises on read-only backends).

        NOTE: The canonical verb is ``unregister_agent`` (matching 19 existing
        unregister_* methods in the framework). The original issue text used
        'deregister_agent' — that form is INCORRECT and must not be used.

        FilesystemAgentRegistryBackend raises RegistrationNotSupported because
        it is a discovery-only (read-only) backend.

        Args:
            agent_id: the string id (folder name) to remove from the registry.

        Raises:
            RegistrationNotSupported: when the backend does not support writes.
        """
        ...

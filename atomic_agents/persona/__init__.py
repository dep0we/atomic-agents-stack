"""PersonaBackend Protocol surface (spec/33 -- #62 PR 1 of 4).

Persona is identity. AgentProfile is configuration. PersonaBackend ships as a
separate Protocol because persona has an independent lifecycle from agent
config: persona is shareable across agents (one SOUL.md for N agents), it is
versionable independently of agent config, and persona templates are
conceptually distinct from agent-config templates (operators bring their own
model and tools).

The composition shape (locked in D1): PersonaBackend is source of truth when
an agent's persona is owned by PersonaBackend (signaled by the presence of
``<agent>/persona.link.md`` in PR 3). AgentProfile fields are denormalized
snapshots populated at ``load_profile()`` time. PR 2 wires the bootstrap
path.

PR 1 of 4 (this PR): **scaffolding only -- NO consumption.** Canonical
dataclasses, Protocol contract, ``FilesystemPersonaBackend`` reference impl,
registry primitives, parametrized conformance suite. No call site routes
through the Protocol yet; ``AtomicAgent.__init__`` is unchanged; all 166
existing construction sites see byte-identical pre-#62 behavior.

The Protocol surface mirrors the established 9-backend pattern (Memory, LLM,
Judge, Lock, Log, AgentProfile, ToolRegistry, Mandate, Policy) -- filesystem
reference impl first, alternate backends (Postgres, SaaS, git) register via
``register_persona_backend(backend_id, cls)`` post-arc without forking core.

Persona exceptions are re-exported here from ``atomic_agents.exceptions`` for
ergonomic access. They live in the cross-module ``exceptions.py`` (per D-PI-1)
because ``PersonaOwnershipConflict`` is raised by profile backends and
``PersonaLinkInvalid`` is raised by the ``persona_link_md.py`` parser -- both
outside the ``persona/`` module.

See ``docs/spec/33-persona-backend.md`` for the full normative contract.
"""

from atomic_agents.exceptions import (
    PersonaCorrupted,
    PersonaError,
    PersonaExists,
    PersonaLinkInvalid,
    PersonaNotFound,
    PersonaOwnershipConflict,
    PersonaSnapshotNotFound,
)

from atomic_agents.persona.backend import (
    PersonaBackend,
    get_default_persona_backend,
    get_persona_backend,
    list_persona_backends,
    register_persona_backend,
    unregister_persona_backend,
)
from atomic_agents.persona.filesystem import (
    FilesystemPersonaBackend,
    make_filesystem_persona_backend_from_url,
)
from atomic_agents.persona.types import (
    Persona,
    PersonaCapabilities,
    PersonaSnapshot,
)

__all__ = [
    # Types
    "Persona",
    "PersonaCapabilities",
    "PersonaSnapshot",
    # Protocol
    "PersonaBackend",
    # Registry primitives
    "get_default_persona_backend",
    "get_persona_backend",
    "list_persona_backends",
    "register_persona_backend",
    "unregister_persona_backend",
    # Reference implementation
    "FilesystemPersonaBackend",
    "make_filesystem_persona_backend_from_url",
    # Exceptions (re-exported from atomic_agents.exceptions per D-PI-1)
    "PersonaCorrupted",
    "PersonaError",
    "PersonaExists",
    "PersonaLinkInvalid",
    "PersonaNotFound",
    "PersonaOwnershipConflict",
    "PersonaSnapshotNotFound",
]

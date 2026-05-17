"""MandateBackend Protocol surface (spec/29 — #124 PR 1 of 6).

Mandates are durable, operator-granted scoped authority records. A mandate
lives in an operator-managed markdown file (``mandates.md``), is referenced
by side-effectful action proposals via ``mandate_id``, and is validated by
the judge layer at action time.

PR 1 of 6 (this PR): scaffolding only — canonical dataclasses, Protocol
contract, ``FilesystemMandateBackend`` reference impl, mandates.md parser,
parametrized conformance suite. No call site routes through the Protocol
yet; ``AtomicAgent.__init__`` is unchanged. PR 2 wires the bootstrap path.

The Protocol surface mirrors the established 7-backend pattern (Memory,
LLM, Judge, Lock, Log, AgentProfile, ToolRegistry) — file-driven reference
impl first, alternate backends (SaaS, mobile, Slack-bot) register via
``register_mandate_backend(name, cls)`` post-arc without forking core. Per
the /office-hours 2026-05-17 Option 2 decision: build the Protocol seam
upfront so heavy-use deployments aren't a retrofit story.

See ``docs/spec/29-mandates.md`` §"Implementer contract for mandate
backends" for the 8 normative MUSTs every implementer commits to.
"""

from atomic_agents.mandate.backend import (
    MandateBackend,
    get_default_mandate_backend,
    get_mandate_backend,
    list_mandate_backends,
    register_mandate_backend,
)
from atomic_agents.mandate.filesystem import (
    FilesystemMandateBackend,
    make_filesystem_mandate_backend_from_url,
)
from atomic_agents.mandate.types import (
    ActionClass,
    Mandate,
    MandateCapabilities,
    MandateConstraints,
    MandateError,
    MandateInvalid,
    MandateNotFound,
    MandateStateSchemaUnsupported,
    ProjectMandateMeta,
    RevocationState,
    TargetPattern,
    TimeWindow,
)
from atomic_agents.mandates_md import parse_mandates_md

__all__ = [
    "ActionClass",
    "FilesystemMandateBackend",
    "Mandate",
    "MandateBackend",
    "MandateCapabilities",
    "MandateConstraints",
    "MandateError",
    "MandateInvalid",
    "MandateNotFound",
    "MandateStateSchemaUnsupported",
    "ProjectMandateMeta",
    "RevocationState",
    "TargetPattern",
    "TimeWindow",
    "get_default_mandate_backend",
    "get_mandate_backend",
    "list_mandate_backends",
    "make_filesystem_mandate_backend_from_url",
    "parse_mandates_md",
    "register_mandate_backend",
]

"""Judge layer — Protocol + registry + canonical types (spec/28).

This package establishes the JudgeBackend Protocol in the protocol-pattern
series alongside MemoryBackend (#57) and LLMBackend (#87). See
``docs/spec/28-judge-layer.md`` for the prose contract.

Public surface (PR 1 of #112 — scaffolding only, no behavior change):

    from atomic_agents.judge import (
        # Protocol contract + outcome model
        JudgeBackend, JudgmentOutcome, Judgment, JudgmentContext,
        # Canonical proposal + audit types
        ActionProposal, ProposalAmendment, ProposalBinding,
        Evidence, Authorization, SkillRef,
        JudgmentEvent,
        # Policy + context types
        JudgePolicyContext, JudgeRuntimeConfig,
        ClassPolicySnapshot, BudgetConfig, EscalationConfig,
        PersonaDigest, ToolPolicyEntry, RunSummary,
        # Enums
        ActionClass, ClassPolicyValue, Reversibility, Provenance,
        # Registry
        register_backend, register_judge_backend,
        get_backend, list_backends, unregister_backend,
    )

The registry is a process-local ``dict`` keyed by judge backend *name*
(e.g., ``"llm"``, ``"rules"``). Threading note: registration is expected
at import time (one-shot from each backend's module); ``get_backend`` is
read-only and safe to call from any thread. No lock is needed under that
usage; matching ``atomic_agents.llm.__init__`` and
``atomic_agents.memory.__init__`` precedent. If a future operator
mutates the registry at runtime from multiple threads, that's their
footgun to sandbox.

Naming: spec/28 §"Registration" shows the public API as
``register_backend`` / ``get_backend`` (unprefixed). We expose both
``register_backend`` (spec-aligned, package-scoped) and
``register_judge_backend`` (alias for unambiguous top-level
``from atomic_agents import register_judge_backend`` consumption,
matching how ``atomic_agents.llm`` exports ``register_llm_backend`` to
avoid colliding with ``atomic_agents.memory.register_backend`` at the
top level).

No default backends register at import time in PR 1. The two reference
implementations (``PolicyJudge``, ``LLMJudgeBackend``) land in PR 2 and
will register themselves via lazy default init the same way
``atomic_agents.llm.anthropic`` does today.
"""

from __future__ import annotations

import logging

from ..exceptions import UnknownJudgeBackendError
from .atomic_action import (
    canonical_tool_definition as atomic_action_tool_definition,
    extract_atomic_action_markers,
)
from .backend import JudgeBackend, Judgment, JudgmentOutcome
from .proposal import (
    assemble_proposal,
    compute_arguments_hash,
    compute_tool_definition_hash,
    is_framework_managed_tool,
)
from .rules import PolicyJudge, make_default_policy_judge
from .types import (
    ActionClass,
    ActionProposal,
    Authorization,
    BudgetConfig,
    ClassPolicySnapshot,
    ClassPolicyValue,
    EscalationConfig,
    Evidence,
    JudgePolicyContext,
    JudgeRuntimeConfig,
    JudgmentContext,
    JudgmentEvent,
    NoteRef,
    PersonaDigest,
    ProposalAmendment,
    ProposalBinding,
    Provenance,
    Reversibility,
    RunSummary,
    SkillRef,
    ToolPolicyEntry,
)

_logger = logging.getLogger(__name__)


__all__ = [
    # Protocol contract + outcome model
    "JudgeBackend",
    "JudgmentOutcome",
    "Judgment",
    "JudgmentContext",
    # Canonical proposal types
    "ActionProposal",
    "ProposalAmendment",
    "ProposalBinding",
    "Evidence",
    "Authorization",
    "SkillRef",
    # Audit shape
    "JudgmentEvent",
    # Policy + context types
    "JudgePolicyContext",
    "JudgeRuntimeConfig",
    "ClassPolicySnapshot",
    "BudgetConfig",
    "EscalationConfig",
    "PersonaDigest",
    "ToolPolicyEntry",
    "RunSummary",
    # Enums
    "ActionClass",
    "ClassPolicyValue",
    "Reversibility",
    "Provenance",
    # Re-export from atomic_agents.memory for convenience (the judge's
    # cited_notes use NoteRef metadata-only per progressive disclosure)
    "NoteRef",
    # Registry
    "register_backend",
    "register_judge_backend",
    "get_backend",
    "get_judge_backend",
    "list_backends",
    "unregister_backend",
    "unregister_judge_backend",
    # Side-channel marker (#112 PR 2a)
    "atomic_action_tool_definition",
    "extract_atomic_action_markers",
    # Proposal assembly (#112 PR 2a)
    "assemble_proposal",
    "compute_arguments_hash",
    "compute_tool_definition_hash",
    "is_framework_managed_tool",
    # Reference impl (#112 PR 2a)
    "PolicyJudge",
    "make_default_policy_judge",
]


# Process-local registry. Keyed by backend *name* (e.g., "llm", "rules").
# Stores the registered instance (not the class) — matches
# ``atomic_agents.llm.__init__`` precedent so operators can pre-configure
# a backend with constructor args before registering.
_registry: dict[str, JudgeBackend] = {}


def register_backend(name: str, backend: JudgeBackend) -> None:
    """Register a ``JudgeBackend`` instance under ``name``.

    Typically called once at module-import time from each backend's
    package (see ``atomic_agents/judge/llm.py`` and
    ``atomic_agents/judge/rules.py`` — landing in PR 2 of #112).

    Re-registering the same name replaces the existing backend silently
    — intentional. Operators occasionally want to swap in a wrapper
    (e.g., a retrying / budgeted variant) without first unregistering
    the original. The replace semantics let them do that with a single
    call.

    Raises ``TypeError`` when ``backend`` doesn't conform to the
    ``JudgeBackend`` Protocol via ``isinstance`` runtime check (method
    presence; not signature). Raises ``ValueError`` when ``name`` is
    empty or whitespace.
    """
    if not name or not name.strip():
        raise ValueError(
            f"judge backend name must be a non-empty string; got {name!r}"
        )
    if not isinstance(backend, JudgeBackend):
        raise TypeError(
            f"backend {backend!r} does not satisfy JudgeBackend "
            f"Protocol (missing required methods)"
        )
    if name in _registry:
        _logger.debug("replacing registered judge backend for name=%r", name)
    _registry[name] = backend


def register_judge_backend(name: str, backend: JudgeBackend) -> None:
    """Alias for ``register_backend`` to avoid collision with other
    sub-package ``register_backend`` symbols at top-level import.

    Matches the ``register_llm_backend`` naming convention from
    ``atomic_agents.llm``. Prefer the unprefixed ``register_backend``
    inside ``atomic_agents.judge`` itself per spec/28; use this alias
    when importing from the top level::

        from atomic_agents import register_judge_backend
    """
    register_backend(name, backend)


def get_judge_backend(name: str) -> JudgeBackend:
    """Alias for ``get_backend`` to avoid collision with
    ``atomic_agents.llm.get_backend`` at top-level import.

    Prefer the unprefixed ``get_backend`` inside ``atomic_agents.judge``
    itself per spec/28; use this alias when importing from the top
    level::

        from atomic_agents import get_judge_backend
    """
    return get_backend(name)


def unregister_backend(name: str) -> None:
    """Remove a backend by ``name``. No-op when not registered.

    Useful for test isolation (``@pytest.fixture(autouse=True)``
    snapshot/restore) and for operators temporarily swapping a backend.
    """
    _registry.pop(name, None)


def unregister_judge_backend(name: str) -> None:
    """Alias for ``unregister_backend`` for symmetry with
    ``register_judge_backend`` / ``get_judge_backend`` at top-level
    import. Prefer the unprefixed ``unregister_backend`` inside
    ``atomic_agents.judge``."""
    unregister_backend(name)


def get_backend(name: str) -> JudgeBackend:
    """Return the registered backend for ``name``, or raise
    ``UnknownJudgeBackendError``.

    The framework's runtime dispatch (PR 2) calls ``get_backend`` after
    parsing the operator's ``judges.md`` to resolve which backend to
    invoke. Per spec/28 §"Registration", the canonical surface is
    ``register_backend`` / ``get_backend``.

    Raises ``UnknownJudgeBackendError`` with a message listing the
    registered set so operators know what's available — mirrors the
    ``UnknownModelError`` pattern from ``atomic_agents.llm``.
    """
    if name not in _registry:
        registered = sorted(_registry.keys())
        raise UnknownJudgeBackendError(
            f"no judge backend registered under name {name!r}. "
            f"Registered: {registered}"
        )
    return _registry[name]


def list_backends() -> list[str]:
    """Return a sorted list of registered backend names.

    Useful for doctor checks (`spec/27`'s `check_judge_health` —
    landing in PR 2) and for `gh issue`-shaped diagnostic prose.
    """
    return sorted(_registry.keys())

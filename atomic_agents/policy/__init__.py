"""PolicyBackend Protocol surface (spec/32 — #89 PR 1 of 4).

Policy is a fleet-wide, configuration-time settings layer. An operator with a
fleet of agents authors a single ``<project_root>/policy.md`` file declaring
fleet-default cost caps, tool allowlists, MCP server allowlists, and model
selection — with optional per-agent overrides under a nested ``agents:``
section. The framework composes Policy with per-agent ``model.md``
cost_guardrails (MIN per dimension), with ``agent.call(cost_cap=...)`` per-call
ceilings (MIN), with Mandate per-action cumulative budgets (MIN), and with
allowlist semantics (AND-compose, deny-takes-precedence within a layer).

PR 1 of 4 (this PR): **scaffolding only — NO consumption.** Canonical
dataclasses, Protocol contract, ``FilesystemPolicyBackend`` reference impl
with mtime+size-gated parse cache, ``policy.md`` parser supporting fleet
defaults + per-agent overrides, parametrized conformance suite across
filesystem + mock backends. No call site routes through the Protocol yet;
``AtomicAgent.__init__`` is unchanged; all 115 existing construction sites
see byte-identical pre-#89 behavior. PR 2 wires the bootstrap path.

The Protocol surface mirrors the established 8-backend pattern (Memory, LLM,
Judge, Lock, Log, AgentProfile, ToolRegistry, Mandate) — filesystem reference
impl first, alternate backends (Postgres, SaaS, org-admin-console) register
via ``register_policy_backend(backend_id, cls)`` post-arc without forking
core. Per the /office-hours 2026-05-19 + /plan-eng-review 2026-05-19 locked
decisions: build the Protocol seam upfront so heavy-use deployments aren't a
retrofit story.

See ``docs/spec/32-policy-backend.md`` for the full normative contract.
"""

from atomic_agents.policy.backend import (
    PolicyBackend,
    get_default_policy_backend,
    get_policy_backend,
    list_policy_backends,
    register_policy_backend,
    unregister_policy_backend,
)
from atomic_agents.policy.filesystem import (
    FilesystemPolicyBackend,
    make_filesystem_policy_backend_from_url,
)
from atomic_agents.policy.types import (
    CostCaps,
    PolicyCapabilities,
    PolicyDecision,
    PolicyError,
    PolicyInvalid,
)
from atomic_agents.policy_md import (
    AgentPolicyOverride,
    PolicySnapshot,
    parse_policy_md,
)

__all__ = [
    "AgentPolicyOverride",
    "CostCaps",
    "FilesystemPolicyBackend",
    "PolicyBackend",
    "PolicyCapabilities",
    "PolicyDecision",
    "PolicyError",
    "PolicyInvalid",
    "PolicySnapshot",
    "get_default_policy_backend",
    "get_policy_backend",
    "list_policy_backends",
    "make_filesystem_policy_backend_from_url",
    "parse_policy_md",
    "register_policy_backend",
    "unregister_policy_backend",
]

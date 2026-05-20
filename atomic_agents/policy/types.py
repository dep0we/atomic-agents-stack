"""Canonical types for the PolicyBackend Protocol (spec/32).

The framework's policy-enforcement surface — cap composition across fleet /
agent / model_md / per-call layers, tool and MCP allow/deny composition,
model selection overrides, and the audit-trail event schema — talks to
policy backends only through these canonical types.  Each backend translates
between its native primitives (a ``policy.md`` file on the filesystem, a row
in a SaaS database, a record in a management plane API) and the canonical
types at its call boundary.

Scaffolding PR (#89 PR 1): no call site routes through the Protocol yet,
and ``AtomicAgent.__init__`` is unchanged.  PR 2 wires the bootstrap path;
the canonical types exist so PR 2 has a stable contract to wire against.

Three design notes that shape the canonical types:

1. **``CostCaps`` is no-opinion-by-default.**  A ``CostCaps()`` with no
   fields set is the correct return value when ``policy.md`` is absent,
   when the agent is not mentioned in the ``agents:`` section, and when no
   fleet-default ``cost_caps`` block exists.  ``None`` at any field means
   "this layer has no opinion on that dimension."  The PR-3 cap compositor
   takes the ``MIN`` of all non-None values across fleet, agent-override,
   model_md, and per-call layers per plan-eng-review D2.

2. **``PolicyCapabilities`` is the honesty contract.**  Backends declare
   their ``cache_ttl_s`` (operator-observable upper bound on staleness at
   the API boundary) and ``durable`` flag.  The filesystem reference impl
   returns ``cache_ttl_s=0`` — operators observe edits within 0 seconds of
   mtime change because the backend performs an mtime+size check on every
   method call.  Dynamic backends (Postgres, SaaS) declare their real
   internal TTL.  Backends that lie produce silent failures rather than loud
   refusals (spec/32 §"Implementer contract" MUST #5).

3. **``PolicyDecision`` schema is stable from day 1.**  SaaS adapters and
   Postgres adapters target this discriminated-union event shape from their
   first line of code; bending the field set after PR 1 breaks them.  The
   ``decision_kind`` / ``axis`` pair is the discriminator — filter on
   ``decision_kind`` first, then ``axis``, then read axis-specific fields.
   Fields not relevant to the axis are ``None``.

Validation of operator-supplied fields (agent_name regex, cap sign, allow/
deny coherence) lives in the parser + backend; the dataclasses here are
storage and wire shapes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Literal

from atomic_agents.exceptions import AtomicAgentsError

if TYPE_CHECKING:
    from ..logs.backend import LogBackend


# ──────────────────────────────────────────────────────────────────────────
# Exceptions
#
# Policy-specific exceptions extend AtomicAgentsError to maintain the
# framework's exception hierarchy.  Operators catching AtomicAgentsError
# catch policy failures too; operators catching PolicyError catch only
# the policy subset.


class PolicyError(AtomicAgentsError):
    """Base class for policy subsystem errors."""


class PolicyInvalid(PolicyError):
    """Raised when policy.md fails parser-level validation.

    Examples: malformed YAML, agent_name outside ``[a-zA-Z0-9_-]+`` at API
    boundary, negative cost-cap value, both allow and deny present for a
    tool name within the same layer (deny still wins, but the parser surfaces
    the ambiguity as a warning — explicit ``PolicyInvalid`` is for structural
    failures only).
    """


# ──────────────────────────────────────────────────────────────────────────
# CostCaps


@dataclass(frozen=True)
class CostCaps:
    """Per-dimension USD cost caps for an agent under Policy.

    Each dimension is independent.  Per plan-eng-review D2: effective cap
    for each dimension is ``MIN(fleet, agent-override, model_md, per-call)``.
    ``None`` at any dimension means "no opinion at this layer."

    v1 ships daily + monthly only.  The ``cumulative_usd`` dimension from
    the original RFC has been deferred to v1.1 (plan-subagent D1) so the
    shipped surface matches ``model.md cost_guardrails`` dimensions exactly.

    A ``CostCaps()`` (no fields set) is the no-opinion default returned by
    ``FilesystemPolicyBackend`` when ``policy.md`` is absent or the agent is
    not mentioned in the ``agents:`` section (and no fleet-default
    ``cost_caps`` block exists).
    """

    daily_usd: float | None = None
    monthly_usd: float | None = None


# ──────────────────────────────────────────────────────────────────────────
# PolicyCapabilities


@dataclass(frozen=True)
class PolicyCapabilities:
    """Backend-declared capability snapshot.

    Per plan-subagent F1 reconciliation: ``cache_ttl_s`` is the
    "operator-observable upper bound on staleness at the API boundary."  The
    filesystem reference impl returns ``0`` — operators observe edits within
    0 seconds of mtime change (mtime+size check on every method call).
    Dynamic backends (Postgres, SaaS) declare their real internal TTL.

    ``cache_ttl_s=None`` means "no staleness contract — backend is expected
    to be authoritative on every read."  ``cache_ttl_s=0`` means "fresh on
    every call as observed at the API boundary."  Positive values are the
    upper bound in seconds.

    Conformance suite gates capability-specific tests on these flags.
    Backends that lie produce silent failures rather than loud refusals
    (spec/32 §"Implementer contract" MUST #5).
    """

    cache_ttl_s: int | None = 0
    durable: bool = True


# ──────────────────────────────────────────────────────────────────────────
# PolicyDecision


@dataclass(frozen=True)
class PolicyDecision:
    """Audit-trail event schema for Policy-related decisions.

    SCHEMA STABILITY: this dataclass is part of the PolicyBackend Protocol
    contract from PR 1.  PR 3 emits instances; SaaS / Postgres adapters
    mirror exactly.  Field set is frozen for v1.

    ``decision_kind`` discriminator:

    - ``"deny"``: some layer (Policy / Mandate / model_md / per_call) denied
      the action.  ``denying_layer`` names which.
    - ``"override"``: Policy returned a non-None model selection that
      supersedes model.md.  ``denying_layer`` is ``None`` (no denial
      occurred).

    ``axis`` field names the surface:

    - ``"cost_cap"``: denial driven by cap composition; ``cap_dimension``,
      ``attempted_value``, ``effective_cap`` populated.
    - ``"tool_allowlist"``: denial driven by tool allow/deny composition;
      ``tool_name`` populated.
    - ``"mcp_allowlist"``: denial driven by MCP server allow/deny
      composition; ``mcp_server_name`` populated.
    - ``"model_selection"``: Policy model override applied; ``model_from_md``
      and ``model_from_policy`` populated; ``denying_layer`` is ``None``.

    ``enforced`` semantics:

    - ``True``: the denial or override blocked / altered the action.  Cost-cap
      denials always emit ``enforced=True`` — they enforce immediately.
    - ``False``: log-only mode — the denial or override was recorded but the
      action proceeded (or the model.md model was used despite an override
      being present).  Non-cap surfaces (tools / MCP / model) emit
      ``enforced=value_of_ATOMIC_AGENTS_POLICY_ENFORCE_NONCAP``; that env-var
      and those surfaces ship in PR 3b.

    Fields not relevant to the axis are ``None``.  Operators reading the
    audit log filter by ``decision_kind`` first, then ``axis``.
    """

    decision_kind: Literal["deny", "override"]
    denying_layer: Literal["policy", "mandate", "model_md", "per_call"] | None
    agent_name: str
    axis: Literal["cost_cap", "tool_allowlist", "mcp_allowlist", "model_selection"]

    # axis-specific (None when not relevant):
    cap_dimension: Literal["daily", "monthly", "per_call"] | None = None
    attempted_value: float | None = None
    effective_cap: float | None = None
    tool_name: str | None = None
    mcp_server_name: str | None = None
    model_from_md: str | None = None
    model_from_policy: str | None = None

    # enforcement flag — always True for cost-cap denials (PR 3a);
    # PR 3b populates based on ATOMIC_AGENTS_POLICY_ENFORCE_NONCAP for
    # non-cap surfaces (tools / MCP / model).
    enforced: bool = False

    # common:
    cache_ttl_s: int | None = None  # backend capability snapshot at decision time
    ts: datetime | None = None
    proposal_id: str | None = None


# ──────────────────────────────────────────────────────────────────────────
# PolicySnapshotForCall


@dataclass(frozen=True)
class PolicySnapshotForCall:
    """Per-call frozen view of Policy state, taken at ``agent.call()`` entry.

    Per design Premise 3 ("snapshot at call entry"): every consumption site
    within the same ``agent.call()`` reads from THIS snapshot — operator edits
    to ``policy.md`` mid-call are deferred to the NEXT call.  Predictability
    over freshness within the call.

    Fields:

    ``effective_caps``: MIN-composed Policy caps for THIS agent (Policy layer
    only; model_md + per-call layers are MIN'd inside
    ``_check_cost_guardrails`` at check time).

    ``cache_ttl_s``: backend capability snapshot at call-entry time.  Included
    so audit events can carry the staleness contract without re-querying the
    backend after call entry.

    ``tool_allow_fn``, ``mcp_allow_fn``, ``model_override``: captured for
    snapshot-shape parity with PR 3b.  PR 3a does NOT consume these fields —
    they are ``None`` / always-allow stubs here.  PR 3b populates and uses
    them when it wires tool / MCP / model surfaces.
    """

    effective_caps: CostCaps
    cache_ttl_s: int | None = None
    # PR 3b fields — captured in snapshot shape but unconsumed in PR 3a:
    tool_allow_fn: Callable[[str], bool] | None = None
    mcp_allow_fn: Callable[[str], bool] | None = None
    model_override: str | None = None


# ──────────────────────────────────────────────────────────────────────────
# Emission helper


def _emit_policy_decision(
    decision: PolicyDecision,
    log_backend: "LogBackend",
    *,
    run_id: str,
) -> None:
    """Append a ``policy_decision`` audit event to ``log_backend``.

    Builds a ``RunRecord`` from ``decision`` and calls
    ``log_backend.append(record)``.  The record uses the ``policy_decision``
    primitive so operators can filter with
    ``LogQuery(primitive="policy_decision")``.

    Called by ``_check_cost_guardrails`` for cost-cap denials (PR 3a).
    PR 3b calls it for tool / MCP / model surfaces.
    """
    # Deferred import to avoid top-level circular dependency:
    # policy/types.py → logs/types.py is safe; logs/types.py does NOT import policy.
    from ..logs.types import PRIMITIVE_POLICY_DECISION, RunRecord

    ts_dt = decision.ts or datetime.now(timezone.utc)
    ts_str = ts_dt.isoformat()
    extra: dict = {
        "decision_kind": decision.decision_kind,
        "denying_layer": decision.denying_layer,
        "axis": decision.axis,
        "enforced": decision.enforced,
    }
    if decision.cap_dimension is not None:
        extra["cap_dimension"] = decision.cap_dimension
    if decision.attempted_value is not None:
        extra["attempted_value"] = decision.attempted_value
    if decision.effective_cap is not None:
        extra["effective_cap"] = decision.effective_cap
    if decision.tool_name is not None:
        extra["tool_name"] = decision.tool_name
    if decision.mcp_server_name is not None:
        extra["mcp_server_name"] = decision.mcp_server_name
    if decision.model_from_md is not None:
        extra["model_from_md"] = decision.model_from_md
    if decision.model_from_policy is not None:
        extra["model_from_policy"] = decision.model_from_policy
    if decision.cache_ttl_s is not None:
        extra["cache_ttl_s"] = decision.cache_ttl_s
    if decision.proposal_id is not None:
        extra["proposal_id"] = decision.proposal_id

    record = RunRecord(
        primitive=PRIMITIVE_POLICY_DECISION,
        trigger="policy_decision",
        agent_name=decision.agent_name,
        run_id=run_id,
        ts=ts_str,
        model="n/a",
        input_tokens=0,
        output_tokens=0,
        status="recorded",
        summary=f"policy_decision: {decision.decision_kind} axis={decision.axis} layer={decision.denying_layer}",
        extra=extra,
    )
    log_backend.append(record)

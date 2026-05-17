"""Canonical types for the MandateBackend Protocol (spec/29).

The framework's mandate-validation surface — `MandateCheck` judge specialist
running on every action proposal citing a mandate, the cost reservation
pattern, the lifecycle event audit trail — talks to mandate backends only
through these canonical types. Each backend translates between its native
primitives (a `<scope>/mandates.md` file on the filesystem, a row in a SaaS
database, a record in a mobile-app store, a Slack message in a channel) and
the canonical types at its call boundary.

Scaffolding PR (#124 PR 1): no call site routes through the Protocol yet,
and `AtomicAgent.__init__` is unchanged. PR 2 wires the bootstrap path; the
canonical types exist so PR 2 has a stable contract to wire against.

Three design notes that shape the canonical types:

1. **`Mandate` is frozen.** Backends MUST NOT mutate a returned `Mandate`
   between `list_mandates()` and `load_mandate()`; the dataclass is
   `@dataclass(frozen=True)` to enforce this at the type level. Operators
   who want to revoke a mandate edit `mandates.md` and the next agent run
   observes the change via the state-file dedup transition (per spec/29
   §"Lifecycle event deduplication").

2. **`MandateCapabilities` is the honesty contract.** Backends declare
   which capabilities they support (revocation observation, external
   state-change notifications, etc); the parametrized conformance suite
   gates capability-specific tests on the flag. Backends that lie produce
   silent failures rather than loud refusals — see spec/29 §"Implementer
   contract for mandate backends" MUST #8.

3. **`ProjectMandateMeta` parses the `_meta` section discipline.** Only
   project-root `mandates.md` honors `_meta`; per-agent files with a
   `_meta` section emit a doctor warning. This type captures the parsed
   `_meta` shape; the parser refuses misplaced metas at load time.

Validation of operator-supplied fields (mandate_id regex, target patterns,
budget arithmetic) lives in the parser + backend; the dataclasses here
are storage shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as dt_time
from enum import Enum
from typing import Literal

from atomic_agents.exceptions import AtomicAgentsError


# ──────────────────────────────────────────────────────────────────────────
# Exceptions
#
# Mandate-specific exceptions extend AtomicAgentsError to maintain the
# framework's exception hierarchy. Operators catching AtomicAgentsError
# catch mandate failures too; operators catching MandateError catch only
# the mandate subset.


class MandateError(AtomicAgentsError):
    """Base class for mandate subsystem errors."""


class MandateInvalid(MandateError):
    """Raised when a mandate fails parser-level validation.

    Examples: malformed YAML in a mandate section, ID outside the
    ``[a-z0-9][a-z0-9-]*`` charset, constraint without
    ``unconstrained: true`` justification, time window with
    ``start_utc >= end_utc``, project-root + per-agent ID collision (per
    spec/29 §"Resolution rules").
    """


class MandateNotFound(MandateError):
    """Raised when ``load_mandate(id, scope)`` cannot resolve the id.

    Distinct from ``MandateInvalid`` (which signals a parse-level
    failure on a known mandate). Distinct from ``BackendNotRegistered``
    (which signals an operator-config failure before any mandate lookup
    happens).
    """


class MandateStateSchemaUnsupported(MandateError):
    """Raised when ``read_state(scope)`` returns a state with an unknown
    ``schema_version``.

    Forward-incompat error per spec/29 §"Lifecycle event deduplication"
    schema_version discipline — readers MUST consult the field and
    raise rather than silently migrate. Operators upgrading across a
    schema bump need an explicit migration step; the failure is loud
    so they cannot accidentally drop state.
    """


# ──────────────────────────────────────────────────────────────────────────
# Enums


class RevocationState(str, Enum):
    """Lifecycle state of a mandate as observed at load time.

    - ``ACTIVE``: mandate is in force; ``MandateCheck`` validates against it.
    - ``REVOKED``: operator set ``revocation_state: revoked`` in
      ``mandates.md``; the mandate no longer authorizes actions.
    - ``EXPIRED``: derived state — framework computes from current time vs
      ``expires_at``. **`mandates.md` itself is never edited** when a
      mandate expires; the framework infers it at load time and emits the
      ``mandate_expired`` lifecycle event on the transition.
    """

    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class ActionClass(str, Enum):
    """Action class for cap-breach behavior (spec/28 + spec/29 alignment).

    Re-exported here for ``MandateConstraints.action_class`` typing
    convenience; the canonical definition lives in spec/28's judge layer.
    Imported as ``str, Enum`` rather than ``StrEnum`` (Python 3.10
    compatibility — ``StrEnum`` only landed in 3.11).
    """

    READ_ONLY = "read_only"
    REVERSIBLE_WRITE = "reversible_write"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    HIGH_RISK = "high_risk"
    IRREVERSIBLE = "irreversible"


# ──────────────────────────────────────────────────────────────────────────
# Sub-dataclasses


@dataclass(frozen=True)
class TargetPattern:
    """A target-matching pattern from a mandate's constraints.

    Currently supports exact-match strings and prefix-match patterns
    (``foo.*``). Future expansion: regex, CIDR (for IP-shaped targets),
    glob — all opt-in via ``kind``.

    Operators supply target patterns as strings in ``mandates.md``; the
    parser converts to ``TargetPattern`` instances. Match semantics live
    in ``MandateCheck`` (PR 3a), not on the dataclass.
    """

    pattern: str
    kind: Literal["exact", "prefix"] = "exact"


@dataclass(frozen=True)
class TimeWindow:
    """A time-of-day window during which a mandate-citing action is allowed.

    Both bounds are UTC. ``start_utc`` and ``end_utc`` are time-of-day
    (no date component); ``MandateCheck`` evaluates against the current
    UTC time. If ``start_utc < end_utc``, the window is contiguous (e.g.,
    09:00–17:00 UTC). If ``start_utc >= end_utc``, the window wraps
    midnight (e.g., 22:00–06:00 UTC, common for off-hours operations).
    The parser refuses ``start_utc == end_utc`` as ambiguous.

    Day-of-week and date-range filtering are out of scope for v1; if an
    operator needs "Mondays only" they use a different mandate per day.
    """

    start_utc: dt_time
    end_utc: dt_time


@dataclass(frozen=True)
class MandateConstraints:
    """The set of validation constraints `MandateCheck` evaluates.

    A mandate without any constraint fields (and without
    ``unconstrained: true``) is rejected by the parser per spec/29
    §"Constraint enforceability (refused at load time)". Operators who
    want a scope-only authorization (no tool / target / budget caps)
    must explicitly set ``unconstrained: true`` with a justification
    string the parser records and the doctor surfaces.

    All ``_usd`` fields are USD-denominated. The mandate budget is
    independent of (and additive to) the actor's overall cost guardrail
    (spec/09); a mandate cap exhausted does NOT exhaust the actor cap.
    """

    # Tool allowlist — set of tool names this mandate authorizes
    allowed_tools: frozenset[str] = field(default_factory=frozenset)

    # Target allow/block lists — patterns the framework's per-agent
    # target_extractor must produce a match against (allowed_targets)
    # or NOT produce a match against (blocked_targets). See spec/29
    # §"Target extraction" for the per-agent named extractor registry.
    allowed_targets: tuple[TargetPattern, ...] = ()
    blocked_targets: tuple[TargetPattern, ...] = ()

    # Time-of-day window — UTC, optional
    time_window: TimeWindow | None = None

    # Token budgets — cumulative LLM cost incurred by actions citing this
    # mandate. Independent of (and additive to) the actor's cost guardrail.
    daily_token_usd: float | None = None
    monthly_token_usd: float | None = None
    cumulative_token_usd: float | None = None

    # External budgets — tool-reported real-money cost (per spec/29 §"Cost
    # integration"). Requires the tool to register expected_external_cost_usd
    # or a cost_estimator callback; otherwise MandateCheck fails-closed at
    # step 8 with reason mandate_external_cost_unprojectable.
    daily_external_usd: float | None = None
    monthly_external_usd: float | None = None
    cumulative_external_usd: float | None = None

    # Escalation thresholds — when projected cost would exceed these,
    # MandateCheck ESCALATEs instead of ALLOW (deferring to operator
    # judgment via the spec/28 ESCALATE machinery)
    requires_escalation_above_token_usd: float | None = None
    requires_escalation_above_external_usd: float | None = None

    # Action class for cap-breach action (per spec/29 §"Validation steps"
    # Budget-breach action). Default external_side_effect (BLOCK).
    action_class: ActionClass = ActionClass.EXTERNAL_SIDE_EFFECT

    # Unconstrained escape hatch — operator opts out of enforcement
    # explicitly with a justification. Doctor surfaces via
    # check_mandate_unconstrained.
    unconstrained: bool = False
    unconstrained_justification: str | None = None


@dataclass(frozen=True)
class Mandate:
    """A durable, operator-granted scoped authority record.

    Loaded by ``MandateBackend.load_mandate(id, scope)``; surfaces
    through the ``MandateCheck`` judge specialist; cited by action
    proposals via ``Authorization.granted_by = "mandate:<id>"``.

    The ``source_hash`` field is the framework-computed canonical hash
    of the mandate's section in ``mandates.md`` (for filesystem backend)
    or the equivalent canonical representation (for SQL / API backends).
    `MandateCheck` step 2 (source hash check) compares this against the
    hash bound at proposal time to detect operator edits between
    proposal and judgment — see spec/29 line 354 + §"Suspicious-rebind
    throttle" for the security shape.

    The ``scope`` field carries either ``agent:<name>`` or
    ``project:<name>`` per spec/29 §"Per-agent vs project-root
    resolution". The scope determines:
      - which state file MandateCheck reads for dedup
      - which set of agents see the mandate (project-root mandates apply
        to all agents in the project; agent-local mandates apply to
        that agent only)
      - which lock-file path is used for high-risk mandates (per
        spec/29 §"High-risk lock specification", PR 4)
    """

    mandate_id: str
    scope: str  # "agent:<name>" | "project:<name>"
    granted_by: str
    granted_at: datetime
    expires_at: datetime | None
    revocation_state: RevocationState
    revoked_at: datetime | None
    revoked_by: str | None
    revocation_reason: str | None
    constraints: MandateConstraints
    source_hash: str  # framework-computed canonical hash
    source_path: str | None = None  # backend-specific source attribution


@dataclass(frozen=True)
class ProjectMandateMeta:
    """Parsed `_meta` section from a project-root `mandates.md`.

    Per-agent files with a `_meta` section emit a doctor warning
    (`check_mandate_meta_misplaced`) and the section is ignored. The
    parser surfaces the warning; this dataclass represents the parsed
    shape that applies to the project-root scope.
    """

    # Per-agent mandate policy: open | listed | forbidden
    per_agent_mandate_policy: Literal["open", "listed", "forbidden"] = "open"

    # When per_agent_mandate_policy=="listed", these are the allowed IDs
    allowed_per_agent_ids: frozenset[str] = field(default_factory=frozenset)


# ──────────────────────────────────────────────────────────────────────────
# Capabilities


@dataclass(frozen=True)
class MandateCapabilities:
    """Backend-declared capability snapshot (spec/29 §"Implementer contract"
    MUST #8: capability honesty is the load-bearing invariant).

    Backends declaring a capability ``True`` MUST implement the
    corresponding behavior such that the parametrized conformance suite's
    capability-gated tests pass. Backends declaring ``False`` get the
    capability-specific tests skipped (not silently passed).
    """

    # Whether the backend observes revocation_state transitions on the
    # persisted mandate. Reference filesystem backend: True (reads
    # mandates.md on every load_mandate; observes operator edits).
    # SQL backend (future): True if the backend re-reads the row each
    # time; False if it caches between writes.
    supports_revocation: bool = True

    # Whether the backend emits subscribe-shape callbacks on out-of-
    # process operator edits (mobile-app update, SaaS UI edit). Reference
    # filesystem backend: False (operator edits surface on the next agent
    # run via state-file dedup, not via push). Future SaaS backends:
    # True (push-on-change).
    supports_external_state_change_notification: bool = False

    # Whether the backend's persistence layer is durable across process
    # restarts. Reference filesystem backend: True. In-memory test backends:
    # False.
    durable: bool = True

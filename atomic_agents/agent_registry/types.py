"""Canonical types for the AgentRegistryBackend Protocol (spec/51).

AgentRegistryBackend is the twenty-second backend Protocol in the atomic-agents
framework (v2.0 wave). It provides fleet-level agent enumeration and governance
metadata so the dashboard and operator tooling can discover agents without relying
on the log/-presence heuristic (which excludes newly-deployed agents with no runs yet).

This module is a leaf: imports only from stdlib (dataclasses, typing); a late
..exceptions import lives inside GovernanceRecord.from_dict() for circular safety.
It MUST NOT import from ..agent, .._llm, .._costs, or any module that transitively
imports those — so it forms no import cycle with the LLM stack (circular-import
safety). NOTE: importing the package still triggers atomic_agents/__init__.py,
which eagerly loads the LLM stack; the boundary here is cycle-safety, not lazy-load.

GovernanceRecord schema:
    Structured fields live in ONE embedded fenced yaml block in governance.md.
    Free-prose lists (forbidden actions, failure modes, pause/retire, sources)
    live as markdown body sections under h2 headers.

    Structured fields are Literal-typed closed unions — unknown enum values at
    parse time produce GovernanceParseError (never silent misread, per the
    spec/51 §"governance.md schema" ruling). The filesystem caller catches that
    error and surfaces it fail-soft via parse_errors (spec/51 MUST 5).

See docs/spec/51-agent-registry-backend.md for the full normative contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal


# ──────────────────────────────────────────────────────────────────
# Governance enum vocabularies
# Maintained as frozensets alongside the Literal definitions so the
# parser can validate without importing typing utilities.

PERMISSION_TIERS = frozenset({"read-only", "draft-only", "writes", "sends-or-acts"})
TRISTATES = frozenset({"yes", "no", "partial"})
LIFECYCLE_STATUSES = frozenset({"active", "paused", "deprecated", "retired"})

PermissionTier = Literal["read-only", "draft-only", "writes", "sends-or-acts"]
Tristate = Literal["yes", "no", "partial"]
LifecycleStatus = Literal["active", "paused", "deprecated", "retired"]


# ──────────────────────────────────────────────────────────────────
# Governance sub-dataclasses (frozen)


@dataclass(frozen=True)
class ReviewRecord:
    """Review/approval metadata parsed from governance.md.

    Fields are optional because governance.md may be partial or in-progress.
    """

    reviewer: str | None = None
    reviewed_at: str | None = None  # ISO-8601 date string
    approved_by: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewRecord":
        """Construct from a dict (e.g. parsed YAML). Unknown keys are silently ignored."""
        return cls(
            reviewer=d.get("reviewer"),
            reviewed_at=d.get("reviewed_at"),
            approved_by=d.get("approved_by"),
        )

    def to_dict(self) -> dict:
        """Convert to a plain dict (lossless round-trip)."""
        return {
            "reviewer": self.reviewer,
            "reviewed_at": self.reviewed_at,
            "approved_by": self.approved_by,
        }


@dataclass(frozen=True)
class RiskRecord:
    """Risk classification parsed from governance.md."""

    level: str | None = None  # e.g. "low", "medium", "high"
    notes: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "RiskRecord":
        return cls(
            level=d.get("level"),
            notes=d.get("notes"),
        )

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class SourcesRecord:
    """Data-source classification parsed from governance.md."""

    primary: list[str] = field(default_factory=list)
    secondary: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "SourcesRecord":
        return cls(
            primary=list(d.get("primary") or []),
            secondary=list(d.get("secondary") or []),
        )

    def to_dict(self) -> dict:
        return {
            "primary": list(self.primary),
            "secondary": list(self.secondary),
        }


@dataclass(frozen=True)
class ActionsRecord:
    """Action-boundary classification parsed from governance.md."""

    permitted: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "ActionsRecord":
        return cls(
            permitted=list(d.get("permitted") or []),
            forbidden=list(d.get("forbidden") or []),
        )

    def to_dict(self) -> dict:
        return {
            "permitted": list(self.permitted),
            "forbidden": list(self.forbidden),
        }


# ──────────────────────────────────────────────────────────────────
# GovernanceRecord


@dataclass(frozen=True)
class GovernanceRecord:
    """Parsed governance metadata for one agent (spec/51).

    Populated from governance.md when present and parseable. Fields marked
    Optional may be None when the governance.md omits them.

    The ``parse_errors`` tuple is non-empty when governance.md was present but
    had validation problems (e.g. unknown permission_tier). A non-empty
    parse_errors tuple means the record carries ONLY parse_errors — every other
    field is reset to its default (owner=None, permission_tier=None, ...), NOT
    preserved from the partially-valid YAML. Callers must surface a warning and
    must not read any enum-typed field on such a record. NOTE: ``from_dict()``
    does NOT itself produce this record — it RAISES GovernanceParseError on the
    first invalid enum (so a YAML block with two bad enums reports only the
    first). The filesystem caller (_parse_governance) catches that error and
    reconstructs the record as ``GovernanceRecord(parse_errors=(str(exc),))`` —
    discarding any fields that DID parse. (spec/51:197 sanctions this
    drop-everything behavior; a field-preserving variant is future work.)

    Frozen dataclass: all fields are immutable after construction.
    Use from_dict() for parsing; direct construction for testing.

    Required fields first (no default): none here — all optional because
    governance.md may be a freshly-generated stub.
    """

    # Structured fields (from embedded YAML block)
    owner: str | None = None
    backup_owner: str | None = None
    permission_tier: PermissionTier | None = None
    customer_data: Tristate | None = None
    writes_sor: Tristate | None = None  # SOR = system of record
    lifecycle_status: LifecycleStatus | None = None
    created_at: str | None = None  # ISO-8601 date
    updated_at: str | None = None  # ISO-8601 date

    # Nested sub-dataclasses (from embedded YAML sub-keys)
    review: ReviewRecord | None = None
    risk: RiskRecord | None = None
    sources: SourcesRecord | None = None
    actions: ActionsRecord | None = None

    # Validation state: non-empty means the YAML block had unknown enum values.
    # Each entry is a human-readable description of the problem.
    parse_errors: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, d: dict) -> "GovernanceRecord":
        """Construct from a parsed YAML dict. Unknown keys are silently ignored.

        Enum fields are validated against their Literal sets. The FIRST invalid
        enum value RAISES GovernanceParseError (fail-fast, not silent default) —
        this method never returns a partial record with populated parse_errors.
        Building the PARTIAL record (has_governance=True + parse_errors) is the
        caller's responsibility: the governance.md parser in filesystem.py
        catches GovernanceParseError and reconstructs a GovernanceRecord with
        parse_errors=(str(exc),). On the success path parse_errors is always ().
        """
        from ..exceptions import GovernanceParseError  # late import for circular safety

        def _validate_enum(
            value: str | None, allowed: frozenset, field_name: str
        ) -> str | None:
            if value is None:
                return None
            # PyYAML (YAML 1.1) coerces the bare words yes/no/on/off/true/false
            # to Python bools, so an operator who writes the DOCUMENTED tristate
            # value `customer_data: no` (per the governance.md template comment
            # "yes / no / partial") yields the bool False, not the string "no".
            # Coerce booleans back to their canonical tristate spelling before
            # validation so the documented vocabulary works whether or not the
            # operator quoted it — without this, the happy-path value silently
            # produces a PRESENT_INVALID record that discards EVERY other field
            # (incl. permission_tier), gutting the governance record (#607).
            # The two tristate fields are the only enums whose vocabulary
            # overlaps YAML's boolean words; permission_tier/lifecycle_status
            # values are never YAML booleans, so this only ever fires for the
            # tristate footgun it exists to fix.
            if isinstance(value, bool):
                value = "yes" if value else "no"
            if value not in allowed:
                raise GovernanceParseError(
                    f"governance.md field {field_name!r} has invalid value {value!r}. "
                    f"Allowed values: {sorted(allowed)}"
                )
            return value

        permission_tier = _validate_enum(
            d.get("permission_tier"), PERMISSION_TIERS, "permission_tier"
        )
        customer_data = _validate_enum(
            d.get("customer_data"), TRISTATES, "customer_data"
        )
        writes_sor = _validate_enum(d.get("writes_sor"), TRISTATES, "writes_sor")
        lifecycle_status = _validate_enum(
            d.get("lifecycle_status"), LIFECYCLE_STATUSES, "lifecycle_status"
        )

        review_raw = d.get("review")
        review = (
            ReviewRecord.from_dict(review_raw) if isinstance(review_raw, dict) else None
        )

        risk_raw = d.get("risk")
        risk = RiskRecord.from_dict(risk_raw) if isinstance(risk_raw, dict) else None

        sources_raw = d.get("sources")
        sources = (
            SourcesRecord.from_dict(sources_raw)
            if isinstance(sources_raw, dict)
            else None
        )

        actions_raw = d.get("actions")
        actions = (
            ActionsRecord.from_dict(actions_raw)
            if isinstance(actions_raw, dict)
            else None
        )

        def _coerce_date(value):
            # created_at/updated_at are typed str | None, but PyYAML coerces an
            # UNQUOTED ISO date (e.g. `created_at: 2026-06-24`) to a
            # datetime.date / datetime.datetime. Coerce it back to an isoformat
            # string so the field's declared type holds and to_dict() round-trips
            # cleanly. Mirrors the bool->tristate coercion in _validate_enum.
            if isinstance(value, (date, datetime)):
                return value.isoformat()
            return value

        return cls(
            owner=d.get("owner"),
            backup_owner=d.get("backup_owner"),
            permission_tier=permission_tier,  # type: ignore[arg-type]
            customer_data=customer_data,  # type: ignore[arg-type]
            writes_sor=writes_sor,  # type: ignore[arg-type]
            lifecycle_status=lifecycle_status,  # type: ignore[arg-type]
            created_at=_coerce_date(d.get("created_at")),
            updated_at=_coerce_date(d.get("updated_at")),
            review=review,
            risk=risk,
            sources=sources,
            actions=actions,
            parse_errors=(),  # success path is always clean; caller builds the partial record
        )

    def to_dict(self) -> dict:
        """Convert to a plain dict.

        Round-trips losslessly for VALID records (every field from_dict()
        reads is emitted). ``parse_errors`` is emitted for inspection but is
        NOT reconstructed by from_dict() — from_dict() always sets it to ``()``,
        and the partial (PRESENT_INVALID) record carrying parse_errors is built
        directly by the filesystem caller, never round-tripped through to_dict.
        """
        return {
            "owner": self.owner,
            "backup_owner": self.backup_owner,
            "permission_tier": self.permission_tier,
            "customer_data": self.customer_data,
            "writes_sor": self.writes_sor,
            "lifecycle_status": self.lifecycle_status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "review": self.review.to_dict() if self.review else None,
            "risk": self.risk.to_dict() if self.risk else None,
            "sources": self.sources.to_dict() if self.sources else None,
            "actions": self.actions.to_dict() if self.actions else None,
            "parse_errors": list(self.parse_errors),
        }


# ──────────────────────────────────────────────────────────────────
# AgentRef — the unit returned by list_agents()


@dataclass(frozen=True)
class AgentRef:
    """Lightweight reference to one discovered agent (spec/51 list_agents()).

    The ``id`` field is the agent folder name — equivalent to the string
    returned by AgentProfileBackend.list_agents(). Dashboard callers extract
    it with ``ref.id`` for backward compatibility with the existing
    ``discover_agents()`` → list[str] contract.

    Fields:
        id: agent folder name (equals the string in AgentProfileBackend.list_agents()).
            MUST be a valid folder name (no path separators).
        location: absolute path to the agent folder as a string. Stable across
            calls for a non-moved agent.
        discovered_at: ISO-8601 UTC timestamp of the list_agents() call that
            produced this ref (call-time, non-deterministic across calls).
            Conformance tests MUST NOT compare discovered_at across two calls;
            assert it is a valid ISO-8601 string only.
        has_governance: True when governance.md is present (even if partially
            valid). False when governance.md is absent or unreadable. Always
            False when this ref was produced by list_agents(include_governance=
            False) — the per-agent governance.md read is skipped on that path,
            so the flag reflects "not read", not "absent on disk".
        governance: populated GovernanceRecord when has_governance=True AND
            the YAML block parsed cleanly. None when absent, unreadable, so
            malformed that no fields could be extracted, present-but-no-block,
            or skipped via list_agents(include_governance=False).
    """

    id: str  # required, no default — folder name
    location: str  # absolute path string, required
    discovered_at: str  # ISO-8601 UTC, required
    has_governance: bool = False
    governance: GovernanceRecord | None = None


# ──────────────────────────────────────────────────────────────────
# AgentEntry — the unit returned by get_agent()

# AgentEntry is an alias for AgentRef at this protocol level.
# get_agent() returns Optional[AgentRef] (None on miss), so the name is the same type.
AgentEntry = AgentRef


# ──────────────────────────────────────────────────────────────────
# Capabilities dataclass


@dataclass(frozen=True)
class AgentRegistryCapabilities:
    """Per-backend capability declaration for AgentRegistryBackend (spec/51).

    All capability booleans default to False so new fields can be appended
    without breaking existing instantiation sites.

    Fields:
        backend_id: stable backend identifier (required, no default).
        supports_registration: True when register_agent() / unregister_agent()
            are supported. FilesystemAgentRegistryBackend=False (read-only
            discovery; registration would require a separate sidecar DB).
        supports_canonical_export: True when the backend implements spec/40
            Exportable Protocol. False for FilesystemAgentRegistryBackend in
            PR 1 — a future state-owning backend (e.g. Postgres) may advertise
            True. The spec/40 seam is intentionally left open.
            NOTE: default False is explicit, not inherited from a missing field.
        single_host_only: True when the backend is safe only for single-host
            deployments. FilesystemAgentRegistryBackend=True.
    """

    backend_id: str  # required, no default
    supports_registration: bool = False
    supports_canonical_export: bool = (
        False  # NOTE: future state-owning backend may advertise True
    )
    single_host_only: bool = False

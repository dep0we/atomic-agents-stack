"""Canonical types for the LogBackend Protocol (spec/22).

The framework's logging surface — ``agent.call()``'s ``self._log({...})``,
``outcome._append_iteration_log``, ``eval._write_run_log``, ``dream``'s
manifest writes — talks to log backends only through these canonical
types. Each backend translates between its native primitives (JSONL
lines on a filesystem, rows in SQLite, events to Datadog) and the
canonical types at its call boundary.

Scaffolding PR (#61 PR 1): no call site routes through the Protocol yet,
and ``agent.py``'s 27+ ``self._log({...})`` invocations continue to write
JSONL via ``_io.atomic_append_jsonl`` directly. PR 2 of the arc wires
backends into the call sites; the canonical types exist so PR 2 has a
stable contract to wire against.

All types are ``@dataclass(frozen=True)`` so they are immutable and
comparable by value — safe to pass across the agent / backend /
diagnostic boundary without defensive copying.

The ``RunRecord`` schema is intentionally permissive: required fields
cover what every primitive emits today; common-but-optional fields are
typed as ``Optional[...]``; primitive-specific keys
(``helper_provenance``, ``delegations``, ``tool_calls``, ``proposal_id``,
``judge_id``, etc.) drop into ``extra: dict``. ``from_dict()`` is
permissive on input: unknown keys land in ``extra`` rather than raising.
This is load-bearing for PR 2's read path — the on-disk JSONL has
accumulated heterogeneous fields across multiple arcs and the backend
must round-trip them faithfully.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any


# ────────────────────────────────────────────────────────────────────
# Canonical primitive taxonomy
#
# Every record gets one ``primitive`` value. The vocabulary is open
# (backends MUST accept arbitrary strings — the conformance suite
# asserts a string type, not membership in this set) but the canonical
# values are listed here so callers and dashboards agree on the bucket
# names. PR 2 derives ``primitive`` from the legacy ``trigger`` string
# via a small mapping function with an ``"other"`` fallback.

PRIMITIVE_AGENT_CALL = "agent_call"
PRIMITIVE_OUTCOME_ITERATION = "outcome_iteration"
PRIMITIVE_DREAM = "dream"
PRIMITIVE_EVAL = "eval"
PRIMITIVE_HELPER = "helper"
PRIMITIVE_DELEGATE = "delegate"
PRIMITIVE_TOOL = "tool"
PRIMITIVE_COST_WARNING = "cost_warning"
PRIMITIVE_CAPTURE = "capture"
PRIMITIVE_ESCALATION = "escalation"
PRIMITIVE_JUDGMENT = "judgment"
PRIMITIVE_MANDATE_RESERVATION = "mandate_reservation"  # #124 PR 3b — reservation event family (granted/used/committed/rolled_back/expired/_on_recovery/_external_unverified) all share this primitive so LogQuery(primitive=PRIMITIVE_MANDATE_RESERVATION, mandate_id=...) is an indexed query on SQLite backends and a bounded scan on filesystem
PRIMITIVE_POLICY_DECISION = "policy_decision"  # #89 PR 3a — Policy/Mandate cap denial + model override audit events; PolicyDecision schema in atomic_agents.policy.types
# spec/46 (#544 PR1) — embedding reservation/release audit records.
# ALL FOUR triggers (embed_reservation, embed_release, embed_batch_reservation,
# embed_batch_release) map to this bucket. Embedding bills from EMBEDDING_PRICING
# (isolated from chat PRICING); folding into PRIMITIVE_HELPER or any existing
# bucket is IRREVERSIBLY lossy once records are written — GROUP BY primitive cost
# attribution for embedding spend would be permanently ambiguous (Principle #5).
PRIMITIVE_EMBED = "embed"
PRIMITIVE_OTHER = "other"


# Canonical (required) RunRecord field names. Used by ``from_dict``
# to decide which keys flow into top-level fields vs ``extra``. The
# set is intentionally narrow — only fields the framework reads
# uniformly across primitives. Anything else is primitive-specific
# and stays in ``extra``.
_CANONICAL_FIELDS = frozenset(
    {
        "ts",
        "run_id",
        "primitive",
        "status",
        "summary",
        "model",
        "input_tokens",
        "output_tokens",
        # common-but-optional
        "cost_usd",
        "cost_source",
        "latency_ms",
        "cache_hit_tokens",
        "cache_miss_tokens",
        "mandate_id",
        "parent_run_id",
        "parent_agent",
        "trigger",
        "agent_name",
        "fallback",
        "critical",
        # idempotency dedup fields (spec/45 PR2 — promoted to canonical so
        # SQLite/Postgres backends can index on them and LogQuery can filter).
        "idempotency_key",
        "replayed_run_id",
        # conversation continuity field (spec/47 PR1 — promoted to canonical so
        # SQLite/Postgres backends can index on it and LogQuery can filter).
        # conversation_id is tagged on every terminal JSONL record when agent.call()
        # is invoked with a conversation_id kwarg.
        "conversation_id",
        # workflow correlation field (spec/22 versioned normative addendum, issue #622
        # PR1 — promoted to canonical so SQLite/Postgres backends can index on it and
        # LogQuery can filter). workflow_id is tagged on call-terminal records and
        # child-cost records (helper, embed_cost) when agent.call() is invoked with a
        # workflow_id kwarg. NOT tagged on trigger='delegate' mirror records (those are
        # excluded from aggregate_workflow() cost sums to prevent double-count).
        "workflow_id",
    }
)


@dataclass(frozen=True)
class RunRecord:
    """A single log entry — the unit of work for every LogBackend method.

    Required fields cover what every framework log site emits today
    (``agent.call()``, ``outcome._append_iteration_log``, helpers, tool
    calls, delegates, etc.). Optional fields cover the
    common-but-not-universal columns dashboards read. Anything else —
    primitive-specific rollup arrays (``helper_provenance``,
    ``delegations``, ``tool_calls``), judge fields (``proposal_id``,
    ``judge_id``, ``judgment_outcome``), and so on — drops into
    ``extra``.

    The ``to_dict`` / ``from_dict`` round-trip is byte-shape preserving:
    appending a ``RunRecord`` via ``FilesystemLogBackend.append()`` then
    reading the JSONL line raw and parsing it through the legacy
    ``dashboard.costs._record_from_dict`` reader returns the same
    ``RunRecord`` for fields the legacy reader knows about. This is the
    invariant PR 2 leans on when wiring call sites.

    Fields:
        ts: ISO-8601 timestamp with timezone, e.g.
            ``"2026-05-15T14:33:21.987654-05:00"``. Populated by callers
            at log time (``_log()`` in ``agent.py:3425`` already does
            this). Backends ORDER on this string; ISO-8601 lexicographic
            order matches chronological order for tz-aware records.
        run_id: the agent run's UUID. Cross-primitive links use
            ``parent_run_id`` to associate child records with this id.
        primitive: canonical taxonomy bucket — one of the
            ``PRIMITIVE_*`` constants. Backends MUST accept arbitrary
            strings; the closed set is documentation, not enforcement.
        status: outcome of the work the record describes — ``"ok"``,
            ``"error"``, ``"skipped"``, ``"lock_busy"``, etc.
        summary: short human-readable description (≤200 chars by
            convention; dashboards truncate further).
        model: LLM model id when applicable. ``"n/a"`` (literal string)
            when not — keeps the column non-null for SQL backends in PR 3.
        input_tokens: LLM input token count. ``0`` when not applicable.
        output_tokens: LLM output token count. ``0`` when not applicable.
        cost_usd: cost contributed by this record. Optional — non-LLM
            records (cost_warning, escalation_resolved) MAY omit it.
        cost_source: spec/28 + spec/30 cost-source taxonomy. One of
            ``"actor" | "judge" | "audit"`` or ``None`` (legacy records
            pre-date the field; readers SHOULD treat ``None`` as
            ``"actor"`` for backward compat — see
            ``_costs.sum_cost_for_period``).
        latency_ms: wall-clock duration in milliseconds. Optional.
        cache_hit_tokens: prompt-cached input tokens (Anthropic). Optional.
        cache_miss_tokens: non-cached input tokens. Optional.
        mandate_id: spec/29 mandate authorization id. Optional;
            reserved in ``_costs.py:118`` reader, not yet written today.
        parent_run_id: the run this record is a child of. Set by
            helper, delegate, tool_call, escalation records.
        parent_agent: name of the parent agent. Set by helper/delegate
            child records.
        trigger: LEGACY free-form dispatch key (e.g.,
            ``"escalation_resolved"``, ``"delegate_batch_reservation"``).
            Preserved verbatim from existing records. PR 2 derives
            ``primitive`` from this with a mapping function +
            ``"other"`` fallback.
        agent_name: agent that emitted the record. Optional; the
            backend's enclosing agent root makes this redundant for
            filesystem-shaped storage but useful for multi-agent rollups.
        fallback: True when the record's cost calculation used fallback
            pricing (unknown model). Optional.
        critical: True when the call was made with ``critical=True``
            (bypassing cost guardrails). Optional.
        extra: primitive-specific keys not covered by the canonical
            fields above. Default-factory empty dict; ``from_dict``
            drops unknown keys here.

    Round-trip note: ``to_dict`` puts ``ts`` first (matching today's
    line shape from ``agent.py:3425``) and flattens ``extra`` into
    top-level keys. ``from_dict`` reverses: known keys → fields,
    unknown keys → ``extra``. This means the same JSON object shape on
    disk reads back identically.
    """

    # Required fields
    ts: str
    run_id: str
    primitive: str
    status: str
    summary: str
    model: str
    input_tokens: int
    output_tokens: int

    # Common-but-optional
    cost_usd: float | None = None
    cost_source: str | None = None
    latency_ms: float | None = None
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None
    mandate_id: str | None = None
    parent_run_id: str | None = None
    parent_agent: str | None = None
    trigger: str | None = None
    agent_name: str | None = None
    fallback: bool | None = None
    critical: bool | None = None
    # spec/45 PR2 — idempotency audit fields (spec/22 versioned normative addendum).
    # Absent on normal ok/skipped/lock_busy records; set on keyed runs.
    idempotency_key: str | None = None
    # replayed_run_id: the run_id of the original completed run, on status='deduped'.
    # Absent on ok and in_flight records (no result is served from a prior run).
    replayed_run_id: str | None = None
    # spec/47 PR1 — conversation continuity field (spec/22 versioned normative addendum).
    # Absent on calls that do not supply a conversation_id. Present on EVERY terminal
    # JSONL record when conversation_id is set — all seven sites: ok, dedup,
    # lock_busy, pre-loop cost-skip, in_flight, mid-loop cost-skip, and
    # security-abort. LogQuery.conversation_id filters on this field.
    conversation_id: str | None = None
    # spec/22 versioned normative addendum (issue #622 PR1): workflow correlation field.
    # Absent on calls that do not supply a workflow_id. Present on EVERY terminal JSONL
    # record when workflow_id is set — the same 9 terminal sites as conversation_id
    # (principal-refused, dedup, lock_busy, pre-loop cost-skip, in_flight, mid-loop
    # cost-skip, ok, security-abort, embed-block) PLUS helper records and embed_cost
    # records (the child-cost records aggregate_workflow() must include). (Not every
    # `conversation_id is not None` reference in agent.py is a stamp site — the
    # conversation-backend resolution branch, write-back/load branches, and door gate
    # reference it WITHOUT writing a JSONL stamp — so a raw grep returns more than 9; the
    # parity is "9 record-stamp sites," not "every conversation_id reference." See the
    # spec/22 grep note.) NOT stamped on trigger='delegate' mirror records
    # (coordinator's delegation mirror) — those are excluded from aggregate_workflow() cost
    # sums to prevent double-count. LogQuery.workflow_id filters on this field.
    workflow_id: str | None = None

    # Primitive-specific catch-all
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict suitable for ``json.dumps``.

        ``ts`` lands first so the on-disk JSONL byte shape matches
        what ``agent.py:_log()`` writes today
        (``{"ts": "...", **record}``). Optional fields with value
        ``None`` are omitted from the output — legacy records
        don't carry these keys, and re-emitting them as ``null`` would
        bloat the artifact and confuse dashboards that test
        ``rec.get("cost_source", "actor")``.

        ``extra`` is flattened into top-level keys. Collisions with
        canonical field names are not possible because ``from_dict``
        only routes UNKNOWN keys to ``extra``.
        """
        out: dict[str, Any] = {"ts": self.ts}
        # Required (non-ts) — always present.
        out["run_id"] = self.run_id
        out["primitive"] = self.primitive
        out["status"] = self.status
        out["summary"] = self.summary
        out["model"] = self.model
        out["input_tokens"] = self.input_tokens
        out["output_tokens"] = self.output_tokens
        # Optional — emit only when set.
        if self.cost_usd is not None:
            out["cost_usd"] = self.cost_usd
        if self.cost_source is not None:
            out["cost_source"] = self.cost_source
        if self.latency_ms is not None:
            out["latency_ms"] = self.latency_ms
        if self.cache_hit_tokens is not None:
            out["cache_hit_tokens"] = self.cache_hit_tokens
        if self.cache_miss_tokens is not None:
            out["cache_miss_tokens"] = self.cache_miss_tokens
        if self.mandate_id is not None:
            out["mandate_id"] = self.mandate_id
        if self.parent_run_id is not None:
            out["parent_run_id"] = self.parent_run_id
        if self.parent_agent is not None:
            out["parent_agent"] = self.parent_agent
        if self.trigger is not None:
            out["trigger"] = self.trigger
        if self.agent_name is not None:
            out["agent_name"] = self.agent_name
        if self.fallback is not None:
            out["fallback"] = self.fallback
        if self.critical is not None:
            out["critical"] = self.critical
        # spec/45 PR2: idempotency audit fields. Omit when None (same None-omit
        # pattern as other optionals). The spec/22 addendum requires cost_usd to be
        # ABSENT (not 0.0) on deduped/in_flight records — the call sites OMIT
        # cost_usd entirely from those record dicts; from_dict() coerces the absent
        # key to None and to_dict() omits None, so the on-disk line has no
        # cost_usd key (spec/22 addendum).
        if self.idempotency_key is not None:
            out["idempotency_key"] = self.idempotency_key
        if self.replayed_run_id is not None:
            out["replayed_run_id"] = self.replayed_run_id
        # spec/47 PR1: conversation_id — omit when None (same None-omit pattern
        # as idempotency fields). LogQuery-queryable via canonical field index.
        if self.conversation_id is not None:
            out["conversation_id"] = self.conversation_id
        # spec/22 versioned normative addendum (issue #622 PR1): workflow_id — omit
        # when None (same None-omit pattern as conversation_id). LogQuery-queryable
        # via canonical field index. NOT emitted on trigger='delegate' mirror records
        # (those records never have workflow_id set; see agent.py propagation rules).
        if self.workflow_id is not None:
            out["workflow_id"] = self.workflow_id
        # Flatten extra last so caller's primitive-specific keys appear
        # after canonical fields in the JSONL line.
        for k, v in self.extra.items():
            out[k] = v
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RunRecord":
        """Build a ``RunRecord`` from a plain dict.

        Permissive: unknown keys flow into ``extra`` rather than
        raising. Required keys with missing values use sensible empty
        defaults (``""`` for strings, ``0`` for token counts, the
        ``"other"`` primitive bucket). This matters because the on-disk
        JSONL has heterogeneous shapes accumulated across multiple
        arcs (early records pre-date ``run_id``; the spec/28 records
        added ``cost_source``; spec/29 will add ``mandate_id``).
        Backends MUST be able to read these legacy lines without
        crashing.

        Args:
            d: the parsed JSON object. Keys are case-sensitive.

        Returns:
            A ``RunRecord`` with all fields populated; unknown keys
            preserved in ``extra``.
        """
        # Required fields — fall back to empty defaults rather than
        # raising. PR 2 inspects records via ``query()``; a single
        # malformed line should not abort the whole month walk.
        ts = str(d.get("ts", ""))
        run_id = str(d.get("run_id", ""))
        primitive = str(d.get("primitive", PRIMITIVE_OTHER))
        status = str(d.get("status", ""))
        summary = str(d.get("summary", ""))
        model = str(d.get("model", "n/a"))
        # Token counts — coerce to int with a 0 default. Legacy records
        # may have float or string values; this matches the defensive
        # coercion in ``dashboard/costs.py:184-185``.
        try:
            input_tokens = int(d.get("input_tokens", 0) or 0)
        except (TypeError, ValueError):
            input_tokens = 0
        try:
            output_tokens = int(d.get("output_tokens", 0) or 0)
        except (TypeError, ValueError):
            output_tokens = 0

        # Optional fields — None when absent, defensively coerced
        # when present. ``_coerce_*`` helpers below keep ``from_dict``
        # readable.
        cost_usd = _coerce_optional_float(d.get("cost_usd"))
        cost_source = _coerce_optional_str(d.get("cost_source"))
        latency_ms = _coerce_optional_float(d.get("latency_ms"))
        cache_hit_tokens = _coerce_optional_int(d.get("cache_hit_tokens"))
        cache_miss_tokens = _coerce_optional_int(d.get("cache_miss_tokens"))
        mandate_id = _coerce_optional_str(d.get("mandate_id"))
        parent_run_id = _coerce_optional_str(d.get("parent_run_id"))
        parent_agent = _coerce_optional_str(d.get("parent_agent"))
        trigger = _coerce_optional_str(d.get("trigger"))
        agent_name = _coerce_optional_str(d.get("agent_name"))
        fallback = _coerce_optional_bool(d.get("fallback"))
        critical = _coerce_optional_bool(d.get("critical"))
        # spec/45 PR2 — idempotency audit fields (spec/22 versioned normative addendum).
        # Both are in _CANONICAL_FIELDS so they are excluded from extra on read-back;
        # explicit extraction + passing ensures the dataclass fields are populated.
        idempotency_key = _coerce_optional_str(d.get("idempotency_key"))
        replayed_run_id = _coerce_optional_str(d.get("replayed_run_id"))
        # spec/47 PR1 — conversation continuity field (spec/22 versioned normative
        # addendum). In _CANONICAL_FIELDS so excluded from extra on read-back.
        conversation_id = _coerce_optional_str(d.get("conversation_id"))
        # spec/22 versioned normative addendum (issue #622 PR1): workflow correlation
        # field. In _CANONICAL_FIELDS so excluded from extra on read-back. See the
        # addendum for which records carry this field (terminal sites + helper +
        # embed_cost; NOT trigger='delegate' mirror records).
        workflow_id = _coerce_optional_str(d.get("workflow_id"))

        # Everything not in the canonical set lands in extra.
        extra = {k: v for k, v in d.items() if k not in _CANONICAL_FIELDS}

        return cls(
            ts=ts,
            run_id=run_id,
            primitive=primitive,
            status=status,
            summary=summary,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            cost_source=cost_source,
            latency_ms=latency_ms,
            cache_hit_tokens=cache_hit_tokens,
            cache_miss_tokens=cache_miss_tokens,
            mandate_id=mandate_id,
            parent_run_id=parent_run_id,
            parent_agent=parent_agent,
            trigger=trigger,
            agent_name=agent_name,
            fallback=fallback,
            critical=critical,
            idempotency_key=idempotency_key,
            replayed_run_id=replayed_run_id,
            conversation_id=conversation_id,
            workflow_id=workflow_id,
            extra=extra,
        )

    def replace(self, **changes: Any) -> "RunRecord":
        """Return a copy with the specified fields replaced.

        Convenience wrapper around ``dataclasses.replace`` so callers
        don't need to import it separately.
        """
        return replace(self, **changes)


@dataclass(frozen=True)
class LogQuery:
    """AND-filter spec for ``LogBackend.query`` and ``LogBackend.aggregate``.

    All fields are optional. Only-set fields contribute predicates;
    fields left as ``None`` are not consulted. This matches the
    ``_costs.sum_cost_for_period`` filter shape (``source`` and
    ``mandate_id`` are AND-filters, omitted-when-None) which PR 2 will
    re-route through ``query``.

    Fields:
        run_id: exact-match run id.
        primitive: exact-match string OR a tuple of acceptable
            primitives. The tuple form lets dashboard queries say
            "any LLM-emitting primitive" via
            ``primitive=("agent_call", "helper", "delegate", "tool")``.
        status: exact-match status string.
        model: exact-match model id.
        cost_source: exact-match cost-source taxonomy value. Records
            with ``cost_source is None`` are treated as ``"actor"`` for
            backward compatibility (legacy records pre-date the field).
        mandate_id: exact-match mandate id (spec/29).
        parent_run_id: exact-match parent run — finds all helper /
            delegate / tool / escalation records for a parent run.
        since: inclusive lower bound on ``ts``. ISO-8601 string
            comparison; tz-aware records sort chronologically.
        until: inclusive upper bound on ``ts``.
        limit: maximum number of records to return AFTER sorting
            chronologically. ``None`` means no cap.
    """

    run_id: str | None = None
    primitive: str | tuple[str, ...] | None = None
    status: str | None = None
    model: str | None = None
    cost_source: str | None = None
    mandate_id: str | None = None
    parent_run_id: str | None = None
    # ``agent_name`` filter (#61 PR 3 review-pass — Step 11 P0 #1).
    # Load-bearing for shared-backend deployments where multiple agents
    # write to the same SQLite/Postgres/Datadog instance. Without this
    # filter, cost-guardrail sums and dashboard reads mix records
    # across agents — alice's daily cap warning fires when the fleet
    # hits her cap, dashboard "alice's spend" shows fleet spend
    # stamped with alice. The filesystem-backend default's
    # one-dir-per-agent shape happens to provide this isolation
    # naturally; shared backends require an explicit filter.
    agent_name: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int | None = None
    # spec/22 versioned normative addendum (spec/45 PR2): LogQuery.idempotency_key
    # conforming backends MUST support as an AND-predicate returning only records
    # whose idempotency_key matches. SQLite backends MUST index on this column
    # (idx_idempotency_key). None = no filter (all records).
    idempotency_key: str | None = None
    # spec/22 versioned normative addendum (spec/47 PR1): LogQuery.conversation_id
    # conforming backends MUST support as an AND-predicate returning only records
    # whose conversation_id matches. SQLite backends MUST index on this column
    # (idx_conversation_id). None = no filter (all records).
    conversation_id: str | None = None
    # spec/22 versioned normative addendum (issue #622 PR1): LogQuery.workflow_id
    # conforming backends MUST support as an AND-predicate returning only records
    # whose workflow_id matches. SQLite and Postgres backends MUST index on this
    # column (idx_workflow_id partial index, WHERE workflow_id IS NOT NULL).
    # None = no filter (all records).
    workflow_id: str | None = None


# Canonical aggregation metrics. Backends advertising
# ``supports_aggregation_pushdown=True`` SHOULD push these to native
# primitives (``SUM(cost_usd)`` in SQL, ``sum:cost_usd`` in Datadog).
# Adding a new metric is a Protocol expansion (semver minor).
METRIC_COUNT = "count"
METRIC_SUM_COST_USD = "sum_cost_usd"
METRIC_SUM_INPUT_TOKENS = "sum_input_tokens"
METRIC_SUM_OUTPUT_TOKENS = "sum_output_tokens"
METRIC_AVG_LATENCY_MS = "avg_latency_ms"

VALID_METRICS = frozenset(
    {
        METRIC_COUNT,
        METRIC_SUM_COST_USD,
        METRIC_SUM_INPUT_TOKENS,
        METRIC_SUM_OUTPUT_TOKENS,
        METRIC_AVG_LATENCY_MS,
    }
)


@dataclass(frozen=True)
class LogAggregate:
    """Grouped-aggregate spec for ``LogBackend.aggregate``.

    Why a fixed metric vocabulary (string) instead of a callable: every
    backend that advertises ``supports_aggregation_pushdown=True`` must
    map ``metric`` to a native primitive (``SUM(cost_usd)`` for SQL,
    ``sum:cost_usd`` for Datadog). A callable would force every backend
    to materialize records into the client and aggregate in Python,
    defeating the SQLite/Datadog story. The vocabulary is small and
    extensible via Protocol expansion (semver minor).

    Fields:
        group_by: tuple of ``RunRecord`` field names to group by. The
            backend looks up each field via ``getattr(record, name)``,
            falling through to ``record.extra.get(name)`` for fields
            not on the canonical ``RunRecord`` dataclass (matches the
            Protocol contract — see ``LogBackend.aggregate``). Empty
            tuple ``()`` means "no grouping; return a single value
            keyed by the empty tuple".
        metric: one of the ``METRIC_*`` constants. Backends MUST raise
            ``ValueError`` for unknown metrics (the conformance suite
            pins this — see ``test_aggregate_unknown_metric_raises``).

    SECURITY NOTE — multi-tenant deployments: ``group_by`` fields that
    resolve through ``record.extra`` (e.g., per-tenant identifiers like
    ``api_key``, ``user_id``, ``billing_account`` stashed in extra by
    a primitive) let any caller enumerate all distinct values in the
    backend via the result dict keys. Callers in multi-tenant systems
    MUST validate ``group_by`` field names against an allowlist of
    known-safe canonical fields before passing to ``aggregate()`` —
    or restrict access to ``aggregate`` to operators only. The
    Protocol does not enforce this at the API boundary; PR 2's
    dashboard wiring is the right layer to gate.
    """

    group_by: tuple[str, ...]
    metric: str


@dataclass(frozen=True)
class LogStats:
    """Backend snapshot — diagnostic-only, racy by design.

    Returned by ``LogBackend.stats()``. Used by ``atomic-agents doctor``
    and the dashboard's home tab to surface "how much history is here?"
    without paying a full scan cost. Values may drift between this
    call and any subsequent action; callers MUST NOT use these for
    control flow.

    Fields:
        total_records: total records in the backend. May be expensive to
            compute for large backends — backends MAY return a coarse
            estimate (e.g., line count rounded to nearest 1000) as long
            as the value is monotonic with appends.
        oldest_ts: ``ts`` of the oldest record, or ``None`` for an
            empty backend.
        newest_ts: ``ts`` of the newest record, or ``None`` for an
            empty backend.
        size_bytes: on-disk byte size for filesystem backends; ``None``
            for backends without a meaningful byte size (Datadog, etc).
        records_today: records with ``ts`` in today's UTC date.
        records_this_month: records with ``ts`` in this UTC month.
    """

    total_records: int
    oldest_ts: str | None
    newest_ts: str | None
    size_bytes: int | None
    records_today: int
    records_this_month: int


@dataclass(frozen=True)
class LogCapabilities:
    """Per-backend capability declaration — see Protocol surface in spec/22.

    Conformance tests assert claim-vs-behavior parity: a backend that
    claims ``supports_retention=True`` MUST implement
    ``delete_older_than`` without raising; one that claims
    ``supports_aggregation_pushdown=True`` SHOULD push aggregates to
    native primitives. Honest capabilities let callers fail fast
    against incompatible backends rather than discovering the mismatch
    mid-operation.

    Fields:
        supports_aggregation_pushdown: True when the backend computes
            ``aggregate()`` via native primitives (SQL ``GROUP BY``,
            Datadog rollup) rather than materializing records and
            aggregating in Python. ``FilesystemLogBackend`` is False;
            ``SQLiteLogBackend`` (PR 3) is True.
        supports_streaming: True when ``query`` returns a generator.
            Reserved as a future capability — both PR 1 reference
            backends are False. A Datadog-class backend where the query
            window can span GB would set this True and yield
            ``RunRecord`` objects.
        supports_retention: True when ``delete_older_than`` is
            implemented natively. Backends advertising False MAY raise
            ``NotImplementedError`` from that method. A future
            append-only / immutable-store backend would set this False.
        durable: True when ``append`` reaches a durable medium before
            returning (fsync, replication ack). ``FilesystemLog`` is
            True. A hypothetical memory-only test backend would set
            this False.
    """

    supports_aggregation_pushdown: bool
    supports_streaming: bool
    supports_retention: bool
    durable: bool
    # spec/40 addendum: Exportable Protocol composition.
    # FilesystemLogBackend = True (full JSONL export).
    # SQLite/Postgres backends default False until their export impls ship.
    # Default False so existing instantiation sites without this kwarg keep working.
    supports_canonical_export: bool = False


# ────────────────────────────────────────────────────────────────────
# Internal coercion helpers — defensive ``from_dict`` plumbing.
#
# Existing on-disk JSONL has heterogeneous types (cost_usd as both
# float and string-serialized-float, fallback as both bool and 0/1).
# The framework's legacy reader ``dashboard.costs._record_from_dict``
# does ``float(rec.get("cost_usd", 0.0) or 0.0)`` style coercions;
# these helpers preserve that behavior for ``RunRecord.from_dict``.


def _coerce_optional_str(v: Any) -> str | None:
    """Return a string when ``v`` is non-None, else None.

    Empty strings ARE preserved (returned as ``""``, not converted to
    ``None``) so the ``to_dict → JSON → from_dict`` round-trip is
    byte-identical for records that legitimately carry empty-string
    optional fields. Treating ``""`` as missing would silently destroy
    data in the round-trip — exactly the failure mode the Step 11
    adversarial review caught.
    """
    if v is None:
        return None
    return str(v)


def _coerce_optional_int(v: Any) -> int | None:
    """Return an int when ``v`` is non-None and parseable, else None."""
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _coerce_optional_float(v: Any) -> float | None:
    """Return a float when ``v`` is non-None and parseable, else None."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _coerce_optional_bool(v: Any) -> bool | None:
    """Coerce common bool-like values (True/False, 0/1, ""/"true")."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        # Treat empty strings as None; "true"/"false" case-insensitively.
        if not v:
            return None
        if v.lower() == "true":
            return True
        if v.lower() == "false":
            return False
        return None
    return None

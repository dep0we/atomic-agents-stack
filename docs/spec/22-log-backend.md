# 22 — LogBackend Protocol

**Status:** **DRAFT** (locks at #61 PR 4).
**Origin:** [#61](https://github.com/dep0we/atomic-agents-stack/issues/61).
**Will ship across four PRs:** PR 1 (Protocol scaffolding + filesystem reference impl + conformance suite + DRAFT spec — this PR), PR 2 (wire backend into the 27+ ``self._log`` call sites + ``outcome._append_iteration_log`` + ``_costs.sum_cost_for_period`` + dashboard readers + ``doctor.check_log_backend`` coherence check + operator override surface), PR 3 (``SQLiteLogBackend`` reference impl + cross-primitive run records + parametrized conformance), PR 4 (spec lock-in + ``Implementer contract for queryable backends`` documented + README/CLAUDE.md status refresh).

## Overview

Every ``agent.call()`` writes a JSONL line via ``self._log()`` to
``<agent>/log/YYYY-MM/YYYY-MM-DD.jsonl``. ``outcome._append_iteration_log``,
``eval._write_run_log`` (which writes to ``evals/runs/`` — a sibling
artifact), and ``dream``'s manifest writes all create chronological-append
records with similar shapes. The dashboard's cost tracker, activity
feed, and run history reader all walk these files directly:

* ``dashboard/costs.py:120 load_runs`` walks ``<agent>/log/YYYY-MM/`` for one agent
* ``_costs.py:100 sum_cost_for_period`` walks the same dirs for the cost guardrail check
* ``dashboard/quality.py:296`` walks them for the quality tab
* ``dream.py:281`` walks them for the dream cost rollup

This works perfectly on a single box. It **breaks** the moment the
deployment shape becomes:

* Multi-agent rollups at fleet scale. "Show me all agents' total cost
  this week" walks N agents × ~7 files each — every dashboard render.
* Year-of-history retrieval. Reading 365 daily files to render the
  cost-trend chart gets slow.
* Retention policy enforcement. GDPR / SOC2 / cost-control may demand
  "drop runs older than 90 days." Filesystem requires manual rotation;
  a DB backend does it via ``DELETE WHERE ts < threshold`` + index.
* Remote shipping. Datadog / Loki / Cloud Logging / Postgres-with-pgvector
  ingest via API; the JSONL-on-disk path is hardcoded at ``agent.py:3427``.
* Run history as first-class. Outcomes / dreams / evals each have their
  own walker today; nobody can join "every run on agent X this month"
  across primitives without a unified query layer.

``LogBackend`` is one of the open protocols in the protocol-pattern
series alongside the shipped ``MemoryBackend`` (spec/20), ``LLMBackend``
(spec/31), ``JudgeBackend`` (spec/28), and ``LockBackend`` (spec/21).
Log is the most-data-volume of any framework artifact and the
most-filesystem-coupled read path; abstracting it unblocks
queryability, retention, and remote shipping in one move.

The Protocol is **not** a generic event-store API. It is the minimal
contract the framework needs to satisfy the audit-trail invariant
documented in CLAUDE.md §5 ("Every agent run writes a JSONL line with
a ``run_id``; helper, tool, and delegate calls write child JSONL lines
carrying ``parent_run_id``"). Backends that meet this contract
participate fully in the agent runtime without forking core.

## Module layout

```
atomic_agents/logs/
├── __init__.py        # registry: register_log_backend / get_log_backend / list_log_backends
├── types.py           # canonical types: RunRecord, LogQuery, LogAggregate, LogStats, LogCapabilities
├── backend.py         # LogBackend Protocol contract
└── filesystem.py      # FilesystemLogBackend reference implementation
```

Mirrors ``atomic_agents/locks/{__init__.py, types.py, backend.py, filesystem.py}``
and ``atomic_agents/llm/{__init__.py, types.py, backend.py, anthropic.py}``.
The split into ``types.py`` separate from ``backend.py`` matches the
Lock and LLM modules' shape: canonical types ship without pulling in
the Protocol contract or any reference implementation.

## Canonical types

### ``RunRecord`` — the unit of work

```python
@dataclass(frozen=True)
class RunRecord:
    # Required (universal across every _log() call site today)
    ts: str               # ISO-8601 with tz
    run_id: str
    primitive: str        # canonical taxonomy — see below
    status: str           # "ok" | "error" | "skipped" | "lock_busy" | ...
    summary: str
    model: str            # "n/a" when not applicable
    input_tokens: int
    output_tokens: int
    # Common-but-optional
    cost_usd: float | None
    cost_source: str | None     # spec/28 + spec/30
    latency_ms: float | None
    cache_hit_tokens: int | None
    cache_miss_tokens: int | None
    mandate_id: str | None       # spec/29
    parent_run_id: str | None
    parent_agent: str | None
    trigger: str | None          # LEGACY free-form
    agent_name: str | None
    fallback: bool | None
    critical: bool | None
    # Primitive-specific catch-all
    extra: dict[str, Any]
```

``RunRecord.to_dict()`` flattens ``extra`` into top-level keys and
places ``ts`` first — preserving today's on-disk line shape
(``{"ts": "...", **record}`` from ``agent.py:3425``) byte-for-byte.
This is the **load-bearing PR 2 invariant**: the JSONL written through
the backend reads identically through the legacy
``dashboard/costs._record_from_dict`` parser, so PR 2 can route writes
through the backend without first rewiring the readers.

``RunRecord.from_dict()`` is permissive: unknown keys land in ``extra``;
required keys with missing values use sensible empty defaults. This
matters for the read path — existing on-disk JSONL has heterogeneous
fields accumulated across multiple arcs (pre-``run_id`` records;
pre-``cost_source`` records; pre-``mandate_id`` records).

#### Canonical primitive taxonomy

``primitive`` buckets records into a small open vocabulary:

* ``agent_call`` — top-level ``agent.call()`` runs
* ``outcome_iteration`` — outcome-loop iteration records
* ``dream`` — dream pipeline runs
* ``eval`` — eval suite runs
* ``helper`` — helper-call rollup records
* ``delegate`` — delegate-call rollup records
* ``tool`` — tool-call records
* ``cost_warning`` — cost-guardrail warning emissions
* ``capture`` — memory-capture audit lines
* ``escalation`` — judge-escalation flow records
* ``judgment`` — judge-decision records
* ``other`` — fallback bucket for primitive-derivation misses

Backends MUST accept arbitrary strings — the closed set is
documentation, not enforcement. PR 2 derives ``primitive`` from the
legacy ``trigger`` string via a small mapping function with an
``"other"`` fallback.

### ``LogQuery`` — AND-filter spec

```python
@dataclass(frozen=True)
class LogQuery:
    run_id: str | None
    primitive: str | tuple[str, ...] | None
    status: str | None
    model: str | None
    cost_source: str | None
    mandate_id: str | None
    parent_run_id: str | None
    since: datetime | None
    until: datetime | None
    limit: int | None
```

All fields are optional. Only-set fields contribute predicates;
``None`` fields are not consulted. Mirrors the
``_costs.sum_cost_for_period`` filter shape (``source`` +
``mandate_id`` as AND-filters, omitted-when-None) which PR 2 will
re-route through ``query``.

``cost_source`` has a backward-compatibility special case: records
with ``cost_source is None`` are treated as ``"actor"`` for filter
purposes, matching the legacy reader at ``_costs.py:149-157``.

### ``LogAggregate`` — grouped-aggregate spec

```python
@dataclass(frozen=True)
class LogAggregate:
    group_by: tuple[str, ...]
    metric: str   # one of types.VALID_METRICS
```

Fixed metric vocabulary:

* ``count`` — number of records in the group (``int``)
* ``sum_cost_usd`` — sum of ``cost_usd`` (``float``; ``None`` counted as 0.0)
* ``sum_input_tokens`` / ``sum_output_tokens`` — token-count sums (``int``)
* ``avg_latency_ms`` — mean of non-``None`` latencies (``float``;
  all-``None`` bucket returns ``None``, NOT 0.0)

Why a fixed string vocabulary (not a callable): every backend
advertising ``supports_aggregation_pushdown=True`` MUST map ``metric``
to a native primitive (``SUM(cost_usd)`` for SQL, ``sum:cost_usd`` for
Datadog). A callable would force every backend to materialize records
into the client and aggregate in Python, defeating the SQLite/Datadog
story. The vocabulary is small and extensible via Protocol expansion
(semver minor).

### ``LogStats`` — diagnostic snapshot

```python
@dataclass(frozen=True)
class LogStats:
    total_records: int
    oldest_ts: str | None
    newest_ts: str | None
    size_bytes: int | None       # None for backends without disk shape
    records_today: int
    records_this_month: int
```

Used by ``atomic-agents doctor`` and the dashboard's home tab.
Diagnostic-only (see §"``stats`` is diagnostic-only" below).

### ``LogCapabilities`` — backend capability declaration

```python
@dataclass(frozen=True)
class LogCapabilities:
    supports_aggregation_pushdown: bool
    supports_streaming: bool      # reserved
    supports_retention: bool
    durable: bool
```

Conformance tests assert claim-vs-behavior parity. A backend that
claims ``supports_retention=True`` MUST implement
``delete_older_than`` without raising; one that claims
``supports_aggregation_pushdown=True`` SHOULD push aggregates to
native primitives.

* ``supports_aggregation_pushdown`` — ``FilesystemLogBackend=False``
  (in-memory after ``query()``). ``SQLiteLogBackend`` (PR 3) = ``True``.
* ``supports_streaming`` — reserved. ``False`` for both PR 1 reference
  backends. A Datadog-class backend with GB-spanning query windows
  would set this ``True`` and yield ``RunRecord`` objects.
* ``supports_retention`` — ``True`` when ``delete_older_than`` is
  natively implemented. Append-only / immutable-store backends set
  ``False`` and MAY raise ``NotImplementedError``.
* ``durable`` — ``FilesystemLog=True`` (fsync). A hypothetical
  memory-only test backend = ``False``.

## ``LogBackend`` Protocol surface

```python
@runtime_checkable
class LogBackend(Protocol):
    @property
    def backend_id(self) -> str: ...
    def append(self, record: RunRecord) -> None: ...
    def query(self, filter: LogQuery) -> list[RunRecord]: ...
    def tail(self, n: int) -> list[RunRecord]: ...
    def aggregate(self, filter: LogQuery, agg: LogAggregate) -> dict[tuple, float | int]: ...
    def delete_older_than(self, threshold: datetime) -> int: ...
    def stats(self) -> LogStats: ...
    def capabilities(self) -> LogCapabilities: ...
```

### ``append`` semantics — atomic, durable, order-preserving

A backend implementing ``append`` MUST:

1. **Be atomic.** A crash mid-``append`` MUST NOT corrupt the backend;
   partial records MUST NOT be readable via ``query``. Filesystem
   backends inherit this via ``_io.atomic_append_jsonl`` (single POSIX
   append for typical <1KB lines); SQL backends use ``INSERT`` inside
   a single transaction; remote backends serialize via the network
   primitive's atomicity guarantee.

2. **Persist before returning.** A crash immediately after ``append()``
   returns MUST NOT lose the record. Filesystem backends fsync; SQL
   backends ack the commit; remote backends wait for server ack.

3. **Preserve insertion order within a run.** Two ``append()`` calls
   in sequence MUST appear in that order from ``query()`` when sorted
   by ``ts``. (Records with identical ``ts`` are ordered by insertion.)

4. **NOT mutate the input.** ``RunRecord`` is frozen; backends MUST
   NOT copy-mutate either (e.g., adding to ``record.extra``).

``append`` is NOT idempotent on the input — calling ``append(record)``
twice MUST persist two records, not one. Log deduplication is the
caller's concern (capture dedupes by ``(type, name, body hash)``;
outcome iteration records dedupe by ``iteration``). The conformance
suite pins this — see ``test_append_does_not_dedup``.

### Aggregation pushdown — string metric, not callable

The ``LogAggregate.metric`` field is intentionally a string from a
fixed vocabulary (see §"Canonical types" above), not a callable. The
rationale: every backend advertising
``supports_aggregation_pushdown=True`` MUST map ``metric`` to a native
primitive (SQL ``SUM(cost_usd)``, Datadog ``sum:cost_usd``). A
callable would force every backend to ship records to client memory
and aggregate in Python — defeating the entire point of pushing the
aggregation down to the storage.

The reference ``FilesystemLogBackend`` aggregates in-memory after
``query()`` and advertises ``supports_aggregation_pushdown=False`` —
callers see the cost transparently.

### Retention contract — strict-before, idempotent, atomic

``delete_older_than(threshold)`` MUST:

1. Delete every record with ``ts < threshold`` (strict). Records with
   ``ts == threshold`` survive.
2. Be **idempotent**: a second call with the same threshold deletes 0
   records. The conformance suite pins this via
   ``test_delete_older_than_idempotent``.
3. Be atomic at the record level: a crash mid-deletion MUST NOT leave
   a half-deleted record. Filesystem backends rewrite the partial-day
   file via ``_io.atomic_write``.

Backends MAY raise ``NotImplementedError`` from ``delete_older_than``
when ``capabilities().supports_retention=False``. This is the escape
hatch for append-only / immutable-store backends (Datadog enforces
retention at the org-policy level, not via SDK calls).

### ``stats`` is diagnostic-only — racy by design

``LogStats`` returned by ``stats()`` reflects the backend at the
moment of the call. The state can change between this call and any
subsequent decision; callers MUST NOT use ``stats()`` for control
flow (e.g., "if ``total_records > 1000`` then archive" — use
``query(LogQuery(limit=...))`` for that).

Used by ``atomic-agents doctor`` and the dashboard's home tab to
surface "how much history is here?" without paying a full scan cost.

### No ``scope()`` method — unlike LockBackend

Logs scope by ``agent_root`` only. ``LockBackend`` (spec/21) needs
``scope()`` because dream / memory locks live in different
sub-directories from the agent's main lock and require distinct
namespaces. Logs have no equivalent sub-scope concern: outcome
iteration, dream completion, helper rollup, etc. all write to the
same daily JSONL today and are distinguished by the ``primitive``
field, not by namespace.

## ``FilesystemLogBackend`` — reference implementation

Conforms to the Protocol with the constructor signature
``FilesystemLogBackend(scope_root)``.

* Writes to ``<scope_root>/log/YYYY-MM/YYYY-MM-DD.jsonl`` via
  ``_io.atomic_append_jsonl`` — exact path-shape match to
  ``agent.py:3427``.
* Reads via month-dir walk; cheap date-window prefilter skips months
  outside ``LogQuery.since/until``; per-line ``json.JSONDecodeError``
  is silently skipped (matches legacy reader at ``_costs.py:147``).
* ``tail`` reverse-walks month dirs → day files → lines, accumulating
  to ``n``, then reverses to chronological order.
* ``delete_older_than`` drops whole-file days strictly before the
  threshold and rewrites the partial threshold-day file atomically
  via ``_io.atomic_write``; empty month dirs are cleaned up.
* ``stats`` line-counts files and sums sizes; oldest/newest ``ts``
  derived from first/last lines of earliest/latest files.

Capabilities: ``supports_aggregation_pushdown=False``,
``supports_streaming=False``, ``supports_retention=True``,
``durable=True``.

## Exception surface

* ``ValueError`` — raised by ``aggregate`` for unknown metrics.
  No new exception class; bare ``ValueError`` matches the lock arc's
  "no new exception unless behavior-distinct" rule.
* ``NotImplementedError`` — backends with
  ``supports_retention=False`` MAY raise from ``delete_older_than``.
* ``BackendNotRegistered`` — raised by ``get_log_backend`` and
  ``get_default_log_backend`` for unknown backend_ids.

## Registry

```python
from atomic_agents.logs import (
    register_log_backend, get_log_backend, list_log_backends,
)

register_log_backend("filesystem", FilesystemLogBackend)
cls = get_log_backend("filesystem")            # → FilesystemLogBackend
backend = cls(agent_root)                       # caller instantiates with scope
ids = list_log_backends()                       # ["filesystem"]
```

The registry stores **classes**, not instances (matches LockBackend
spec/21 §Registry). Log backends carry per-scope construction
arguments; the registry's role is the operator-pin lookup that
resolves a backend_id to a class.

The default ``"filesystem"`` registration happens at import time
inside ``atomic_agents/logs/__init__.py``.

## Operator surface — NOT a ``logs.md`` config

Log backend choice is a **deployment-level** decision (the whole
framework instance picks "filesystem" or "sqlite" or "datadog"), not
an agent-author-level decision. Contrast with:

* ``judges.md`` — per-agent because judge policy is per-agent-author concern.
* ``model.md`` ``provider:`` — per-agent because model choice is per-agent.

A ``logs.md`` markdown config would only make sense if multiple log
backends were registered simultaneously and an agent author wanted to
pin one — an unlikely scenario; log backend is operator's call.

PR 2 of #61 will expose the choice via TWO paths (parallel to the
LockBackend operator surface in spec/21 §"Operator surface — NOT a
``locks.md`` config"):

1. **Constructor kwarg** — programmatic operators (Python entry-points
   wiring the framework into Cloud Run, Kubernetes deployments with
   custom Datadog clients) pass
   ``AtomicAgent(..., log_backend=DatadogLogBackend(...))`` to bypass
   the env-var resolution entirely.

2. **Environment variables** — deployment-config operators (Docker,
   launchd, Cloud Run env, gizmo systemd units) set:
   - ``ATOMIC_AGENTS_LOG_BACKEND`` — backend id (default
     ``filesystem``). PR 1 supports ``filesystem``; PR 3 adds
     ``sqlite``.
   - ``ATOMIC_AGENTS_LOG_BACKEND_URL`` — connection / path string for
     non-filesystem backends (e.g., ``sqlite:///path/to/logs.db``,
     ``datadog://api-key-from-secrets``).

The env var name ``ATOMIC_AGENTS_LOG_BACKEND_URL`` is intentionally
generic (not ``_SQLITE_PATH``) so future Datadog / Loki / Postgres
backends plug in via the same key without operators having to
relearn the env vocabulary.

The constructor kwarg ALWAYS wins. Operator-config layering: env vars
are deployment-level (per-instance, per-host); the kwarg is
per-agent-construction. A test that constructs an ``AtomicAgent``
with an explicit ``log_backend=`` bypasses any env vars the
deployment may have set.

## What PR 1 does NOT do

PR 1 ships pure scaffolding — Protocol, filesystem reference impl,
tests, DRAFT spec. **Zero call-site changes.** The 27+
``self._log({...})`` sites in ``agent.py``,
``outcome._append_iteration_log``, ``eval._write_run_log``, the four
dashboard / cost-walker readers (``dashboard/costs.py``,
``_costs.py``, ``dream.py``, ``dashboard/quality.py``) — all untouched.

PR 2 wires:

* ``agent._log()`` becomes a thin wrapper that builds a ``RunRecord``
  from the dict literal (unknown keys → ``extra``) and calls
  ``self.log_backend.append(record)``. The 27 call sites stay verbatim.
* The dashboard / cost / dream / quality readers route through
  ``self.log_backend.query(LogQuery(...))`` instead of walking
  month dirs directly.
* ``AtomicAgent.__init__`` accepts an optional
  ``log_backend: LogBackend | None`` constructor kwarg that, when set,
  bypasses ``get_default_log_backend``.
* ``doctor.check_log_backend`` coherence check validates operator
  config (env var → registry lookup → backend reachability).

PR 3 ships ``SQLiteLogBackend`` (stdlib ``sqlite3``; no optional
extra needed) and parametrizes the conformance suite across both
backends. PR 4 locks this spec and adds the
``§"Implementer contract for queryable backends"`` section.

## Reserved future capabilities

These are not committed in PR 1 but are reserved in the namespace so
future expansions don't need a breaking Protocol change:

* ``AsyncLogBackend`` — async variant for HTTP-served deployments.
  Same shape; ``append`` / ``query`` / ``tail`` become ``async def``.
* ``StreamingLogBackend`` — adds ``stream(filter) -> Iterator[RunRecord]``
  for backends like Datadog where the query window can span GB.
  Reserved because the current materialized-list contract serves
  typical dashboard windows just fine.
* ``RetentionPolicyBackend`` — adds ``set_retention_policy(days)``
  for backends that enforce TTL at the storage level (Datadog policy,
  Postgres partition pruning). Today's operators configure these
  externally; the Protocol expansion lets the framework drive it.

## Conformance test surface

PR 1 ships:

* ``tests/test_log_protocol_conformance.py`` — 30 tests parametrized
  via a ``backend_factory`` fixture, ready to receive PR 3's
  ``SQLiteLogBackend`` factory entry.
* ``tests/test_log_filesystem_backend.py`` — 20 filesystem-specific
  tests (on-disk path mapping, byte-for-byte legacy reader compat,
  ``atomic_append_jsonl`` integration, retention rewrite atomicity,
  multi-file tail walk, ``extra``-field aggregation, registry
  resolution).

PR 4 freezes the conformance surface against both filesystem +
``SQLiteLogBackend`` and locks this spec doc.

## Related

* spec/20 — ``MemoryBackend`` (the original Protocol pattern; this spec mirrors its shape).
* spec/21 — ``LockBackend`` (immediate-sibling template; this spec mirrors its ``types.py``/``backend.py``/registry split and operator-surface rationale).
* spec/28 — ``JudgeBackend`` (third-template; the log arc adopts the same "lock spec at PR 4" discipline).
* spec/31 — ``LLMBackend`` (second-template; this spec mirrors its types/backend split).
* CLAUDE.md §5 — the "Audit trail is structural" rule this Protocol is in service of.

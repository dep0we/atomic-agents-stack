# 22 — LogBackend Protocol

**Status:** **locked** (spec matches implementation as of #61 PR 4).
**Origin:** [#61](https://github.com/dep0we/atomic-agents-stack/issues/61).
**Shipped across four PRs:** PR 1 (Protocol scaffolding + ``FilesystemLogBackend`` reference impl + conformance suite + DRAFT spec — #185), PR 2 (wire backend into the 27+ ``self._log`` call sites + ``outcome._append_iteration_log`` + ``_costs.sum_cost_for_period`` + dashboard readers + ``doctor.check_log_backend`` coherence check + operator override surface — #186), PR 3 (``SQLiteLogBackend`` reference impl + parametrized conformance suite + ``LogQuery.agent_name`` filter for shared-backend cross-agent isolation + URL parsing — #187), PR 4 (spec lock-in + ``Implementer contract for queryable backends`` documented + README/CLAUDE.md status refresh — this PR).

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

1. **Be atomic for records ≤ ``PIPE_BUF``** (POSIX, typically 4096
   bytes). SHOULD be atomic for larger records via backend-native
   serialization (SQL transaction, lease-token-checked Lua,
   single-request HTTP). A crash mid-``append`` MUST NOT leave a
   readable partial record in ``query`` output for records within the
   atomicity bound. The reference ``FilesystemLogBackend`` inherits
   ``_io.atomic_append_jsonl``'s ``PIPE_BUF`` bound; records carrying
   rollup arrays (``helper_provenance``, ``delegations``,
   ``tool_calls``) on the top-level ``agent.call()`` write routinely
   exceed 4 KB. Operators with deployments that generate >4 KB records
   on shared NFS or with multiple processes appending to the same day
   file SHOULD select a transaction-backed backend (``SQLiteLogBackend``
   in PR 3, future Postgres/Datadog impls). Filesystem-default
   deployments on a single host accept the bound; the failure mode is
   silent partial-line append observable as ``json.JSONDecodeError``
   skips in ``query()``.

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
4. **Raise ``ValueError`` on naive datetimes**. Silent local-vs-UTC
   conversion is the failure shape that produces off-by-one-day
   retention errors near midnight; operators MUST pass a tz-aware
   threshold. The conformance suite pins this via
   ``test_delete_older_than_rejects_naive_threshold``.

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
     non-filesystem backends. The concrete URL format is settled by
     each backend at its PR (PR 3 commits ``SQLiteLogBackend``'s
     format; future Datadog / Loki impls settle theirs). PR 1
     reserves the env var name but does NOT pin any URL schema —
     operators on Cloud Run who set the var ahead of PR 3 SHOULD
     consult the backend's spec section for the exact format before
     deploying.

   Credential safety: ``get_default_log_backend`` sanitizes the
   ``ATOMIC_AGENTS_LOG_BACKEND`` value before echoing it in error
   messages — strips anything following ``://`` and truncates at 32
   chars — so an operator who accidentally pastes a URL credential
   into ``ATOMIC_AGENTS_LOG_BACKEND`` (instead of
   ``ATOMIC_AGENTS_LOG_BACKEND_URL``) does not see the credential
   echoed in the resulting ``BackendNotRegistered`` exception text.

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
``§"Implementer contract for queryable backends"`` section below.

## Implementer contract for queryable backends (#61 PR 4)

A backend that claims ``LogCapabilities.supports_aggregation_pushdown=True`` is committing to the indexed-query + native-aggregate pattern documented above. Concretely, **implementers MUST**:

1. **Push every ``LogQuery`` predicate to native query primitives**. SQL backends translate to ``WHERE`` clauses; document-store backends translate to facet filters; remote-API backends translate to query parameters. The filesystem reference impl is the in-memory fallback; pushdown backends MUST NOT materialize records into the client before filtering. Specifically: ``LogQuery.since/until`` MUST become indexed range scans (the ``ts`` column or equivalent MUST be indexed); ``LogQuery.run_id``, ``LogQuery.primitive``, ``LogQuery.parent_run_id``, ``LogQuery.agent_name`` MUST become exact-match equality clauses (each backed by an index for hot-path queries — at minimum ``ts``, ``run_id``, ``primitive``, ``parent_run_id``). The reference ``SQLiteLogBackend`` ships six indexes (``ts``, ``run_id``, ``primitive``, ``parent_run_id``, ``cost_source``, ``mandate_id``) and uses ``EXPLAIN QUERY PLAN`` verification in the test suite to assert index use.

2. **Lenient ``agent_name`` filtering**. Records with ``agent_name IS NULL`` (legacy pre-PR-2 records imported from a filesystem export) MUST match any ``agent_name`` filter — under filesystem's per-agent-dir scoping, every record in the dir IS the named agent's; strict filtering would break dashboard reads of legacy data. Backends translate to ``WHERE (agent_name = :name OR agent_name IS NULL)`` or equivalent. The cross-agent-isolation property holds for explicitly-stamped records (every post-PR-2 record carries ``agent_name`` set by ``agent.py:_log()``).

3. **Backward-compat ``cost_source`` filtering**. Records with ``cost_source IS NULL`` (legacy pre-spec/28 records) MUST be treated as ``"actor"`` for filter purposes — this preserves the cost-guardrail semantic from before the spec/28 actor/judge split. Backends translate to ``WHERE (cost_source = 'actor' OR cost_source IS NULL)`` when the filter is ``"actor"``; strict equality for any other value.

4. **Atomic schema initialization across processes**. Multi-process operators may have N replicas all opening a fresh backend file simultaneously. Schema-creation INSERTs MUST use idempotent patterns (``INSERT OR IGNORE``, ``ON CONFLICT DO NOTHING``, equivalent) so the cold-start race doesn't deadlock a replica. The reference ``SQLiteLogBackend`` learned this in PR 3 review-pass (Step 11 adversarial P0 #2); future backends MUST mirror. SQLite-shape backends additionally MUST set ``PRAGMA busy_timeout=5000`` (or equivalent) BEFORE ``PRAGMA journal_mode=WAL`` AND retry the WAL transition on ``SQLITE_BUSY`` / ``SQLITE_LOCKED`` — the WAL switch acquires an EXCLUSIVE lock that the busy_handler does not always cover, so a fresh-db race on N concurrent threads/processes will surface ``database is locked`` without both defenses (#208 — same shape as spec/25 §"Implementer contract for registry-backed tool backends" MUST #5).

5. **``delete_older_than`` raises ``ValueError`` on naive datetimes**. The spec/22 ``LogBackend`` contract is strict on this; the queryable-backend contract additionally requires that backends do NOT silently convert naive ``→`` UTC ``→`` query parameter. Operators MUST pass tz-aware thresholds to avoid the off-by-one-day retention failure shape near midnight.

6. **Aggregation pushdown — ``group_by`` resolution**. ``group_by`` field names that match canonical ``RunRecord`` columns MUST resolve to direct column references in the native ``GROUP BY``. Field names that resolve only through ``record.extra`` MAY raise ``NotImplementedError`` when the backend's primitive doesn't support JSON-extraction (e.g., a backend without a SQL JSON1 equivalent). When the backend does support JSON extraction, the implementer MUST validate ``group_by`` identifiers against an allowlist of safe identifiers (alphanumeric + underscore, ASCII-only) before interpolating into the native query — the reference ``SQLiteLogBackend`` does this at ``sqlite.py:aggregate()``. Operators wanting ``extra``-field group_bys on a pushdown backend that doesn't support JSON extraction MUST either canonicalize the field by promoting it to ``RunRecord`` (Protocol expansion, semver minor) or use the filesystem reference for that query.

7. **Connection / handle management**. Backends MUST be safe to construct, use, and abandon without explicit ``close()``. A ``release()``-equivalent method is not part of the Protocol because the framework's call-site lifecycle (one ``LogBackend`` instance per ``AtomicAgent`` for the agent's full life) doesn't have a deterministic teardown point. Backends with limited connection pools (Postgres, HTTP) MUST use ``threading.local`` or a connection-pool library that handles thread-life-tied cleanup automatically. The reference ``SQLiteLogBackend`` uses ``threading.local`` for per-thread connections; the WAL journal mode lets the kernel reclaim connections on thread exit without explicit close.

8. **Multi-tenant scoping (deferred to the implementer)**. The Protocol's per-backend ``scope_root`` (passed to the constructor) is the framework's default isolation primitive. Operators who pin a single shared backend across multiple agents rely on ``LogQuery.agent_name`` filtering at the read boundary. Backends with native multi-tenant capabilities (Postgres row-level security, Datadog org tags) MAY enforce additional isolation; the Protocol does not require it but the implementer documentation MUST surface whatever guarantees the backend provides.

The reference ``SQLiteLogBackend`` implementation in ``atomic_agents/logs/sqlite.py`` is the canonical example of this contract. Future Postgres / Datadog / Loki / Cloud Logging adapters should mirror its shape; the conformance suite (``tests/test_log_protocol_conformance.py``) parametrizes across every registered backend so the contract is verified by the same tests that pin ``append`` / ``query`` / ``aggregate`` / ``delete_older_than`` / ``stats`` semantics.

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

The conformance suite (PR 4 lock):

* ``tests/test_log_protocol_conformance.py`` — 46 tests parametrized
  via a ``backend_factory`` fixture across both reference backends
  (``FilesystemLogBackend`` + ``SQLiteLogBackend``). 92 total test
  invocations (46 × 2). Third-party backends import the
  ``BACKEND_FACTORIES`` list to verify their own conformance against
  the same contract. Tests cover: Protocol surface, append semantics
  (persist / no-dedup / no-mutate / empty-string round-trip /
  arbitrary primitive), every query filter (run_id, primitive
  single/tuple, status, model, since/until inclusive boundary,
  sub-second precision, cost_source legacy-actor backward-compat,
  mandate_id, parent_run_id, agent_name strict-isolation +
  lenient-on-legacy, limit, chronological order), tail (chronological-
  LAST, zero, more-than-total, negative-raises, empty-backend),
  aggregate (count, sum_cost_usd, sum_input_tokens int-type,
  sum_output_tokens int-type, unknown-metric ValueError,
  avg_latency None-bucket, empty group_by), retention (removes
  old records, idempotent, strictly-before boundary, empty-backend,
  rejects naive datetime), stats (with records, empty backend),
  capabilities (type + behavior parity).
* ``tests/test_log_filesystem_backend.py`` — 22 filesystem-specific
  tests (on-disk path mapping, byte-for-byte legacy reader compat,
  ``atomic_append_jsonl`` integration, retention rewrite atomicity,
  multi-file tail walk, ``extra``-field aggregation, registry
  resolution, URL credential redaction).
* ``tests/test_log_sqlite_backend.py`` — 31 SQLite-specific tests
  (schema creation + version tracking + version-mismatch refusal +
  cold-start race idempotency, six indexes, WAL journal mode,
  round-trip preserves every RunRecord field with extra JSON,
  EXPLAIN QUERY PLAN index use, cost_source legacy NULL handling,
  delete_older_than SQL pushdown, concurrent multi-threaded
  appends, multi-instance reopen, aggregate group_by via JSON1 +
  SQL injection guard, URL parsing edge cases, in-memory data-loss
  RuntimeWarning, _CANONICAL_COLUMNS derivation from
  RunRecord.__dataclass_fields__, empty-string round-trip
  preservation, registry resolution).
* ``tests/test_log_integration.py`` — 19 wiring integration tests
  pinning ``AtomicAgent.log_backend`` public attribute + kwarg
  override, primitive derivation from legacy trigger, byte-for-byte
  legacy-reader compat at the end-to-end boundary, OutcomeRunner +
  DreamRunner kwarg threading, sum_cost routing, dashboard load_runs
  routing, count_provenance wiring, doctor PASS/FAIL/URL-redaction.

Total: 124 LogBackend-arc tests + 92 parametrized invocations =
**216 test runs** verifying the Protocol contract.

## Related

* spec/20 — ``MemoryBackend`` (the original Protocol pattern; this spec mirrors its shape).
* spec/21 — ``LockBackend`` (immediate-sibling template; this spec mirrors its ``types.py``/``backend.py``/registry split and operator-surface rationale).
* spec/28 — ``JudgeBackend`` (third-template; the log arc adopts the same "lock spec at PR 4" discipline).
* spec/31 — ``LLMBackend`` (second-template; this spec mirrors its types/backend split).
* CLAUDE.md §5 — the "Audit trail is structural" rule this Protocol is in service of.

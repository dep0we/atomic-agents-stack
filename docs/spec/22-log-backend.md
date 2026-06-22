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
This is a **load-bearing invariant**: the JSONL written through
the backend reads identically through the legacy
``dashboard/costs._record_from_dict`` parser, so the backend routes writes
without first rewiring the readers.

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
documentation, not enforcement. ``primitive`` is derived from the
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
``mandate_id`` as AND-filters, omitted-when-None) which is
re-routed through ``query``.

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
  (in-memory after ``query()``). ``SQLiteLogBackend`` = ``True``.
* ``supports_streaming`` — reserved. ``False`` for both reference
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
   file SHOULD select a transaction-backed backend (``SQLiteLogBackend``,
   future Postgres/Datadog impls). Filesystem-default
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
  No new exception class for the original 8-MUST surface; bare
  ``ValueError`` matches the lock arc's "no new exception unless
  behavior-distinct" rule.
* ``NotImplementedError`` — backends with
  ``supports_retention=False`` MAY raise from ``delete_older_than``.
* ``BackendNotRegistered`` — raised by ``get_log_backend`` and
  ``get_default_log_backend`` for unknown backend_ids.
* ``LogBackendReadError(AtomicAgentsError)`` — **added in v1.5 by the
  read-failure posture addendum (issue #497)**. Raised by ``query()`` /
  ``tail()`` / ``aggregate()`` on unrecoverable read failures (disk
  corruption, I/O error, lost database connection after all retries
  exhausted). See §"spec/22 addendum — Read-failure posture" below for
  the full normative boundary and conformance contract.

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

The operator surface exposes the choice via TWO paths (parallel to the
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
     ``filesystem``). Recognized: ``filesystem``, ``sqlite``, ``postgres``.
   - ``ATOMIC_AGENTS_LOG_BACKEND_URL`` — connection / path string for
     non-filesystem backends. ``SQLiteLogBackend``'s URL format is
     committed; future Datadog / Loki impls settle theirs. Operators
     on Cloud Run consult the backend's spec section for the exact
     format before deploying.

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

## Implementer contract for queryable backends

A backend that claims ``LogCapabilities.supports_aggregation_pushdown=True`` is committing to the indexed-query + native-aggregate pattern documented above. Concretely, **implementers MUST**:

1. **Push every ``LogQuery`` predicate to native query primitives**. SQL backends translate to ``WHERE`` clauses; document-store backends translate to facet filters; remote-API backends translate to query parameters. The filesystem reference impl is the in-memory fallback; pushdown backends MUST NOT materialize records into the client before filtering. Specifically: ``LogQuery.since/until`` MUST become indexed range scans (the ``ts`` column or equivalent MUST be indexed); ``LogQuery.run_id``, ``LogQuery.primitive``, ``LogQuery.parent_run_id``, ``LogQuery.agent_name`` MUST become exact-match equality clauses (each backed by an index for hot-path queries — at minimum ``ts``, ``run_id``, ``primitive``, ``parent_run_id``). The reference ``SQLiteLogBackend`` ships six indexes (``ts``, ``run_id``, ``primitive``, ``parent_run_id``, ``cost_source``, ``mandate_id``) and uses ``EXPLAIN QUERY PLAN`` verification in the test suite to assert index use.

2. **Lenient ``agent_name`` filtering**. Records with ``agent_name IS NULL`` (legacy pre-PR-2 records imported from a filesystem export) MUST match any ``agent_name`` filter — under filesystem's per-agent-dir scoping, every record in the dir IS the named agent's; strict filtering would break dashboard reads of legacy data. Backends translate to ``WHERE (agent_name = :name OR agent_name IS NULL)`` or equivalent. The cross-agent-isolation property holds for explicitly-stamped records (every post-PR-2 record carries ``agent_name`` set by ``agent.py:_log()``).

3. **Backward-compat ``cost_source`` filtering**. Records with ``cost_source IS NULL`` (legacy pre-spec/28 records) MUST be treated as ``"actor"`` for filter purposes — this preserves the cost-guardrail semantic from before the spec/28 actor/judge split. Backends translate to ``WHERE (cost_source = 'actor' OR cost_source IS NULL)`` when the filter is ``"actor"``; strict equality for any other value.

4. **Atomic schema initialization across processes**. Multi-process operators may have N replicas all opening a fresh backend file simultaneously. Schema-creation INSERTs MUST use idempotent patterns (``INSERT OR IGNORE``, ``ON CONFLICT DO NOTHING``, equivalent) so the cold-start race doesn't deadlock a replica. The reference ``SQLiteLogBackend`` learned this in PR 3 review-pass (Step 11 adversarial P0 #2); future backends MUST mirror. SQLite-shape backends additionally MUST set ``PRAGMA busy_timeout=5000`` (or equivalent) BEFORE ``PRAGMA journal_mode=WAL`` AND retry the WAL transition on ``SQLITE_BUSY`` / ``SQLITE_LOCKED`` — the WAL switch acquires an EXCLUSIVE lock that the busy_handler does not always cover, so a fresh-db race on N concurrent threads/processes will surface ``database is locked`` without both defenses (#208 — same shape as spec/25 §"Implementer contract for registry-backed tool backends" MUST #5).

5. **``delete_older_than`` raises ``ValueError`` on naive datetimes**. The spec/22 ``LogBackend`` contract is strict on this; the queryable-backend contract additionally requires that backends do NOT silently convert naive ``→`` UTC ``→`` query parameter. Operators MUST pass tz-aware thresholds to avoid the off-by-one-day retention failure shape near midnight.

6. **Aggregation pushdown — ``group_by`` resolution**. ``group_by`` field names that match canonical ``RunRecord`` columns MUST resolve to direct column references in the native ``GROUP BY``. Field names that resolve only through ``record.extra`` MAY raise ``NotImplementedError`` when the backend's primitive doesn't support JSON-extraction (e.g., a backend without a SQL JSON1 equivalent). When the backend does support JSON extraction, the implementer MUST validate ``group_by`` identifiers against an allowlist of safe identifiers (alphanumeric + underscore, ASCII-only) before interpolating into the native query — the reference ``SQLiteLogBackend`` does this at ``sqlite.py:aggregate()``. Operators wanting ``extra``-field group_bys on a pushdown backend that doesn't support JSON extraction MUST either canonicalize the field by promoting it to ``RunRecord`` (Protocol expansion, semver minor) or use the filesystem reference for that query.

7. **Connection / handle management**. Backends MUST be safe to construct, use, and abandon without their data being corrupted or a single abandoned instance leaking unbounded resources within one process lifetime — a ``release()``-equivalent method is not part of the Protocol because the framework's call-site lifecycle (one ``LogBackend`` instance per ``AtomicAgent`` for the agent's full life) doesn't have a deterministic teardown point. Backends with limited connection pools (Postgres, HTTP) MUST scope connections per-thread (e.g. ``threading.local``) or use a connection-pool library. **Two cases:** (a) backends whose driver connections ARE reclaimed by the kernel/runtime on thread exit (e.g. the reference ``SQLiteLogBackend`` under WAL journal mode) need no explicit close; (b) backends whose driver connections are NOT reclaimed on thread exit (e.g. psycopg, whose ``threading.local`` connections persist until ``close()`` or GC ``__del__``) MUST expose a ``close()`` method AND their implementer documentation MUST state that operators with churning worker-thread pools call ``close()`` in teardown to avoid accumulating server-side connections. The reference ``PostgresLogBackend`` is a case-(b) backend (see the non-normative "Connection pool and thread safety" notes below, and the bounded ``psycopg_pool.ConnectionPool`` successor issue #365 for the fleet-scale answer that removes the operator-teardown dependency).

8. **Multi-tenant scoping (deferred to the implementer)**. The Protocol's per-backend ``scope_root`` (passed to the constructor) is the framework's default isolation primitive. Operators who pin a single shared backend across multiple agents rely on ``LogQuery.agent_name`` filtering at the read boundary. Backends with native multi-tenant capabilities (Postgres row-level security, Datadog org tags) MAY enforce additional isolation; the Protocol does not require it but the implementer documentation MUST surface whatever guarantees the backend provides.

The reference ``SQLiteLogBackend`` implementation in ``atomic_agents/logs/sqlite.py`` is the canonical example of this contract. Future Postgres / Datadog / Loki / Cloud Logging adapters should mirror its shape; the conformance suite (``tests/test_log_protocol_conformance.py``) parametrizes across every registered backend so the contract is verified by the same tests that pin ``append`` / ``query`` / ``aggregate`` / ``delete_older_than`` / ``stats`` semantics.

### Postgres implementation notes (non-normative)

These notes are non-normative — they do not extend the MUST count or the spec's
LOCKED status. They document implementation choices that conform to the existing
8-MUST Implementer Contract and serve as a reference for the ``PostgresLogBackend``
implementation in ``atomic_agents/logs/postgres.py`` (Issue #258, PR 1 of N).

**Column types (Postgres-specific)**

| SQLite column          | Postgres equivalent                           | Rationale                                   |
|------------------------|-----------------------------------------------|---------------------------------------------|
| ``id INTEGER AUTOINCREMENT`` | ``id BIGSERIAL PRIMARY KEY``           | Postgres has no AUTOINCREMENT keyword       |
| ``ts TEXT NOT NULL``   | ``ts TEXT NOT NULL``                          | ISO-8601 lex ordering; same TEXT approach   |
| ``cost_usd REAL`` / ``latency_ms REAL`` | ``cost_usd DOUBLE PRECISION`` / ``latency_ms DOUBLE PRECISION`` | SQLite ``REAL`` is float8 (8-byte double); Postgres ``REAL`` is float4 (4-byte single, ~7 sig digits). ``DOUBLE PRECISION`` (float8) preserves cross-backend value fidelity for cost accounting. |
| ``extra TEXT NOT NULL DEFAULT '{}'`` | ``extra JSONB NOT NULL DEFAULT '{}'::jsonb`` | Enables ``->>`` operator; no json_extract |
| ``fallback INTEGER``   | ``fallback BOOLEAN``                          | Postgres native boolean type               |
| ``critical INTEGER``   | ``critical BOOLEAN``                          | Postgres native boolean type               |

**Cold-start race mitigation (MUST 4)**

The Postgres equivalent of SQLite's ``INSERT OR IGNORE`` is two layers:

1. ``SELECT pg_advisory_xact_lock(key)`` at the start of the schema-init
   transaction. The xact-scoped variant (not session-scoped) auto-releases on
   COMMIT/ROLLBACK — no explicit release, no pool-recycle leak.
2. ``INSERT INTO meta ... ON CONFLICT (key) DO NOTHING`` — idempotent
   schema_version row insert. Losing the race is a no-op.

The advisory lock key is a stable ``int8`` derived from
``hashlib.sha256(b"atomic-agents-log-schema-v1").digest()[:8]`` (struct big-endian
signed int64) so all processes target the same key without coordination.

**psycopg 3 paramstyle**

psycopg 3 uses ``%s`` positional placeholders (paramstyle ``'pyformat'``), NOT
``?`` (paramstyle ``'qmark'`` — that is sqlite3). Every parameterized statement
in ``postgres.py`` uses ``%s``. The ``ANY(%s)`` idiom with a list parameter is
the idiomatic Postgres IN-list pattern (avoids N-placeholder string construction).

**JSONB extra column — aggregation**

The SQLite ``json_extract(extra, '$.FIELD')`` expression becomes ``(extra->>'FIELD')``
in Postgres (JSONB text accessor). The SQL injection guard (alphanumeric + underscore
allowlist) applies identically. ``(extra->>'FIELD')`` returns TEXT for all values;
callers needing numeric aggregation on extra fields must CAST explicitly.

**Known cross-backend divergence — extra-field group_by KEY TYPE (#366).**
This is a real, accepted gap in v1.0, called out here rather than left implied:
``aggregate(group_by=(<extra-field>,))`` returns dict keys of a DIFFERENT TYPE
depending on the registered backend. Postgres ``->>`` always yields TEXT; the
Filesystem and SQLite reference backends return the value's NATIVE Python type
(``json_extract`` / dict round-trip). The divergence — and whether the
documented ``str(k)`` mitigation actually bridges it — is per JSON value class
for the SAME data:

| extra value class | Postgres key | SQLite key | Filesystem key | `str(k)` bridges? |
|---|---|---|---|---|
| numeric `{'iteration': 1}` | `('1',)` | `(1,)` | `(1,)` | **Yes** — `str(1) == '1'` |
| float `{'ratio': 1.5}` | `('1.5',)` | `(1.5,)` | `(1.5,)` | **Yes** — `str(1.5) == '1.5'` |
| string `{'env': 'prod'}` | `('prod',)` | `('prod',)` | `('prod',)` | n/a — identical |
| **boolean** `{'flag': True}` | `('true',)` | `(1,)` | `(True,)` | **No** — `'true' != '1' != 'True'` |

So the general "the same records produce the same stats regardless of which
backend is registered" property holds for canonical-column group_bys and for
string extra fields, and is RECOVERABLE via ``str(k)`` for numeric and float
extra fields — but does NOT hold for booleans, where the three backends yield
three distinct string forms (Postgres emits the JSON text literal ``'true'``,
SQLite's ``json_extract`` returns an int, Filesystem round-trips a Python
``bool``) that no single coercion reconciles. ``->>`` cannot know the operator's
intended type, so a blind CAST would be a guess; the divergence is pinned by
``test_aggregate_extra_field_key_type_divergence`` in the conformance suite
(which forces it to be a deliberate edit, not silent drift) and tracked for
remediation by #366. Dashboards grouping on a NUMERIC or FLOAT extra field MUST
coerce keys with ``str(k)`` to stay backend-portable until #366 lands; BOOLEAN
extra fields are NOT backend-portable for group_by under any coercion until #366
lands.

No GIN index on ``extra`` is created — hot append path (27+ ``_log()`` calls per
``agent.call()``); speculative GIN would double write latency. File a successor
issue with a concrete benchmark requirement if JSON-path query performance becomes
load-bearing.

**Connection pool and thread safety**

``threading.local`` with individual ``psycopg.connect()`` calls — one TCP
connection per OS thread per ``PostgresLogBackend`` instance. psycopg 3
connections are NOT thread-safe; per-thread connections are required.
``max_connections_used = N_instances × max_threads_per_instance``. Keep this
below ``Postgres max_connections - 5`` (reserved for admin connections). A
bounded ``psycopg_pool.ConnectionPool`` layer for fleet operators is successor
issue #365.

Unlike SQLite (WAL lets the kernel reclaim per-thread connections on thread
exit), psycopg connections held via ``threading.local`` are NOT released on
thread exit — they persist until ``backend.close()`` is called or GC runs
``__del__``. Operators with churning worker-thread pools MUST call ``close()``
in teardown/shutdown to avoid accumulating server-side connections; the bounded
``psycopg_pool.ConnectionPool`` successor issue #365 is the fleet-scale answer.
This is exactly the case-(b) carve-out in the normative Implementer Contract
MUST 7 above — ``PostgresLogBackend`` is a case-(b) backend, so it exposes
``close()`` and this paragraph is the implementer documentation MUST 7 requires.

**Three-layer credential redaction**

This is the first credentialed-URL backend; every future Postgres/Redis/Loki
adapter copies this contract:

(A) ``_redact_dsn(url)`` strips credentials from any logged/echoed URL.
(B) Connections opened with explicit keyword args (``host=``, ``port=``,
    ``dbname=``, ``user=``, ``password=``) — psycopg never builds a DSN
    string that it can echo internally. ``psycopg`` logger suppressed to
    ``WARNING`` at backend construction.
(C) Credentials come only via ``ATOMIC_AGENTS_LOG_BACKEND_URL`` parsed at
    construction; the full raw URL string is not retained (only the redacted
    ``_safe_url`` is stored). Note: the password component IS stored as the
    ``_password`` instance attribute for driver use — ``__dict__`` / debugger
    introspection can expose it. Wrap in a ``SecretStr`` with a custom
    ``__repr__`` if repr-level protection is required.

**Capability values**

``streaming=False`` — mirrors SQLite. Reserved; file a successor issue with a
named trigger if a streaming path (Datadog-class GB query windows) becomes
needed. ``durable=True``, ``supports_aggregation_pushdown=True``,
``supports_retention=True``.

**size_bytes in stats()**

Always ``None``. Postgres stores data remotely; no local file to stat. The spec
allows ``None`` for backends without a disk shape. Operators who want storage
size can query ``pg_total_relation_size('run_records')`` via psql directly.

**Conformance test count update**

After adding ``PostgresLogBackend`` to ``BACKEND_FACTORIES``, the conformance
suite produces 48 × 3 = 144 parametrized invocations in CI (was 48 × 2 = 96
locally). Verified: ``uv run pytest tests/test_log_protocol_conformance.py
--collect-only -q`` reports 96 collected (48 tests × 2 local backends).
The Postgres factory is gated on ``ATOMIC_AGENTS_TEST_POSTGRES_URL`` env var
so it runs in CI (service container sets the var) and skips locally without
Postgres. ``tests/test_log_postgres_backend.py`` holds 53 Postgres-specific tests:
mock-cursor tests that pin internal SQL generation and connection lifecycle
(run unconditionally), plus real-DB integration tests gated on
``ATOMIC_AGENTS_TEST_POSTGRES_URL`` that exercise Protocol semantics against a
live service container (skipped locally). The conformance contract itself is
verified by the 48 parametrized conformance tests in
``tests/test_log_protocol_conformance.py``, not by this file.

## Reserved future capabilities

These are not committed in v1.0 but are reserved in the namespace so
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

The conformance suite:

* ``tests/test_log_protocol_conformance.py`` — 48 tests parametrized
  via a ``backend_factory`` fixture across the reference backends
  (``FilesystemLogBackend`` + ``SQLiteLogBackend`` locally; plus
  ``PostgresLogBackend`` in CI when ``ATOMIC_AGENTS_TEST_POSTGRES_URL``
  is set). 96 local invocations (48 × 2); 144 in CI (48 × 3).
  Third-party backends import the ``BACKEND_FACTORIES`` list to verify
  their own conformance against the same contract. Tests cover:
  Protocol surface, append semantics
  (persist / no-dedup / no-mutate / empty-string round-trip /
  arbitrary primitive), every query filter (run_id, primitive
  single/tuple, status, model, since/until inclusive boundary,
  sub-second precision, cost_source legacy-actor backward-compat,
  mandate_id, parent_run_id, agent_name strict-isolation +
  lenient-on-legacy, limit, chronological order), tail (chronological-
  LAST, zero, more-than-total, negative-raises, empty-backend),
  aggregate (count, sum_cost_usd, sum_input_tokens int-type,
  sum_output_tokens int-type, unknown-metric ValueError,
  avg_latency None-bucket, empty group_by, two-extra-field group_by,
  numeric-extra-field key-type divergence #366),
  retention (removes old records, idempotent, strictly-before boundary,
  empty-backend, rejects naive datetime), stats (with records, empty
  backend), capabilities (type + behavior parity).
* ``tests/test_log_filesystem_backend.py`` — 22 filesystem-specific
  tests (on-disk path mapping, byte-for-byte legacy reader compat,
  ``atomic_append_jsonl`` integration, retention rewrite atomicity,
  multi-file tail walk, ``extra``-field aggregation, registry
  resolution, URL credential redaction).
* ``tests/test_log_sqlite_backend.py`` — 33 SQLite-specific tests
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
* ``tests/test_log_postgres_backend.py`` — 53 Postgres-specific tests
  (mock-cursor tests for SQL generation, schema init cold-start race,
  advisory lock, credential redaction including query-string credentials,
  unencoded TLS-path preservation, and unencoded-slash / unencoded-hash /
  unencoded-question-mark-in-password (no-leak / no-crash, in both the
  _redact_dsn output and the construction-time ValueError), a credential-less
  URL carrying an '@' in a query value constructs (port-gated detection, not
  refused) while a real special-char password with an explicit port is still
  rejected without leak, threading.local
  isolation, JSONB extra-field round-trip, aggregate JSONB ->> operator,
  cost_usd DOUBLE PRECISION round-trip guard, URL parsing edge cases,
  make_postgres_backend_from_url, close() idempotency lifecycle,
  transparent one-shot reconnect on a not-yet-flagged connection drop,
  at-most-once append (NO retry on a commit-phase connection drop, so a
  lost-commit-ack never doubles an audit row), doctor known-id recognition,
  registry resolution, schema-version-mismatch real-Postgres refusal).
* ``tests/test_log_integration.py`` — 19 wiring integration tests
  pinning ``AtomicAgent.log_backend`` public attribute + kwarg
  override, primitive derivation from legacy trigger, byte-for-byte
  legacy-reader compat at the end-to-end boundary, OutcomeRunner +
  DreamRunner kwarg threading, sum_cost routing, dashboard load_runs
  routing, _count_provenance wiring, doctor PASS/FAIL/URL-redaction.

Total: 137 LogBackend-arc tests + 103 local conformance invocations =
**240 local test runs**. The net delta over the pre-#497 census
(224 → 240, **+16**) breaks down as **conformance +7** (96 → 103: 6 from the
three read-failure tests parametrized over the two local backends — filesystem,
sqlite — + 1 standalone reader-seam test) and **arc +9** (filesystem 22 → 30:
6 read-failure boundary tests pinning the empty-vs-failure errno branches
[dir-level ENOENT → [], month-dir ENOENT skip, per-file ENOENT skip, per-file
non-ENOENT → raise] for query() + 2 tail() skip-branch tests; postgres 54 → 55:
1 connect-time read-error wrap test for tail()/aggregate() — all added during
the #497 ``/ship`` review for coverage completeness). The postgres
``test_read_error_discards_connection_prevents_aborted_tx_trap`` was UPDATED
in place to expect ``LogBackendReadError`` (not added). There are **two**
near-identical reader-seam tests proving ``_sum_via_backend`` fails closed on
``LogBackendReadError``: (1) ``test_sum_via_backend_fails_closed_on_log_backend_read_error``
lives **in this conformance file** (``tests/test_log_protocol_conformance.py``)
and **is** the census's "+1 standalone reader-seam test" counted inside the 103
conformance term; (2) ``test_backend_query_logbackendreaderror_returns_degraded``
lives in ``tests/test_costs.py``, **outside** this arc census. (Both assert the
same fail-closed property from the consumer side; the conformance-file copy
keeps the contract demonstrable from the conformance suite alone, the
``test_costs.py`` copy keeps the cost-reader's own test file self-complete.)
(Mandate spend-gate posture: #497 added an INTERIM fail-closed guard for the
outstanding-reservation read — a typed ``except LogBackendReadError`` at the
step-7/step-8 callers that BLOCKs (``mandate_{token,external}_reservations_unreadable``),
with 2 tests in ``tests/test_mandate_check.py``. The FULL posture shipped in #506 —
the remaining prior-cost-sum sites were flipped from broad-``except`` to a narrow
read-failure catch raising ``BLOCK mandate_cost_unreadable``, with the
spec/29 §"Blind-read fail-closed posture (issue #506)" LOCKED amendment.
Tamper-evidence of the cost log itself — so "unverifiable" subsumes "tampered",
not only "unreadable" — is the **#500** named seam: out of scope for the default
filesystem backend, available to a future real-authz ``LogBackend``; see spec/29
§"Blind-read fail-closed posture (issue #506)".) (Census-honesty note: the pre-#497 census stated postgres as 53 and a
total of 223; both were a pre-existing undercount-by-one — ``--collect-only``
reports postgres at 54 on main, so the true pre-#497 local total was 224. The
figures here are recomputed from ``--collect-only`` and are authoritative.)

<!-- Census arithmetic — recompute from `--collect-only`, do not hand-increment:
     arc (non-conformance) tests = filesystem 30 + sqlite 33 + postgres 55 +
       integration 19 = 137. (filesystem 22 → 30: +8 #497 /ship coverage tests
       [6 query() errno-boundary + 2 tail() skip-branch]; postgres 54 → 55: +1
       #497 connect-time read-error wrap test; the pre-#497 census also
       mis-stated postgres as 53 — corrected to 54 on main.)
     conformance term (local, Postgres-absent) = 96 (48 unique funcs × 2 local
       backends fs+sqlite) + 6 (3 read-failure tests × 2 local backends) + 1
       (standalone reader-seam test) = 103.
     local total = 137 + 103 = 240 (was 224 pre-#497: 128 arc + 96 conformance;
       +16 net = +9 arc [filesystem +8, postgres +1] + +7 conformance [96 → 103]).
     CI/Postgres-present: the postgres backend joins both the 48-func
       parametrization and the 3 read-failure tests, so the conformance term
       becomes 48×3 + 3×3 + 1 = 154 and the Postgres-present command collects
       137 + 154 = 291.
     Verify (local, Postgres-absent — clamp the env var so the count is
     deterministic; an exported ATOMIC_AGENTS_TEST_POSTGRES_URL adds the
     postgres backend at COLLECTION time and changes the total):
     `env -u ATOMIC_AGENTS_TEST_POSTGRES_URL uv run pytest
     tests/test_log_protocol_conformance.py
     tests/test_log_filesystem_backend.py tests/test_log_sqlite_backend.py
     tests/test_log_postgres_backend.py tests/test_log_integration.py
     --collect-only -q | tail -1` -> 240 collected. -->


## Related

* spec/20 — ``MemoryBackend`` (the original Protocol pattern; this spec mirrors its shape).
* spec/21 — ``LockBackend`` (immediate-sibling template; this spec mirrors its ``types.py``/``backend.py``/registry split and operator-surface rationale).
* spec/28 — ``JudgeBackend`` (third-template; the log arc adopts the same "lock spec at PR 4" discipline).
* spec/31 — ``LLMBackend`` (second-template; this spec mirrors its types/backend split).
* CLAUDE.md §5 — the "Audit trail is structural" rule this Protocol is in service of.

## spec/40 addendum — Canonical export

`LogBackend` participates in the **Exportable** companion Protocol (spec/40).

`LogCapabilities.supports_canonical_export = True` for `FilesystemLogBackend`.
`SQLiteLogBackend` and `PostgresLogBackend` default `False` until their export
impls ship.

`export()` returns a `LogExport` carrying `(RunRecord, raw_bytes)` tuples.
Raw bytes are produced by `json.dumps(record.to_dict()).encode("utf-8") + b"\n"` —
ts-first insertion order, NOT `canonical_json()` (which uses `sort_keys=True` and
would break Tier A byte-exact fidelity, spec/40 MUST 8).

For the full normative export contract, see `docs/spec/40-canonical-export.md`.

## spec/22 addendum — Read-failure posture (v2, issue #497)

This versioned normative addendum records the read-failure contract added in
issue #497. The spec remains LOCKED; this addendum is backward-compatible
(new exception class; existing empty/absent behavior unchanged).

**This rule is OUTSIDE the 8-MUST queryable-backend Implementer Contract
count at §"Implementer contract for queryable backends" (heading at line 458;
MUST 8 at line 476) above.** That 8-MUST contract is documented ONLY in
spec/22 itself — there is no `8-MUST` cross-reference in README.md or
CLAUDE.md to keep in sync, so the count remains valid by not touching the
queryable-backend section. This addendum is a BASE Protocol-surface rule
binding ALL backends (including `FilesystemLogBackend`) via the public
exception type, and is delivered as a separate normative subsection rather
than a renumbered MUST #9 (per the lock-ceremony ruling, matching the spec/41
#483 versioned-addendum precedent).

### Read-failure boundary

The **explicit boundary** (reusing spec/09's locked taxonomy):

| Condition | Behavior |
|---|---|
| Backend is absent / empty / log directory does not exist | Return `[]` — MUST NOT raise |
| Directory-level `ENOENT` — `log_dir` (or a month dir) vanished AFTER the `.exists()` check (TOCTOU with retention cleanup / external `rm`) | Return `[]` (top dir) / skip (month dir) — MUST NOT raise; this is the absent-state contract |
| File disappeared between directory listing and `open()` (TOCTOU race — ENOENT on a per-file read) | Skip (continue) — MUST NOT raise |
| Directory-level NON-ENOENT `OSError` — e.g., `PermissionError` on `iterdir()`, `NotADirectoryError`, `EIO` | Raise `LogBackendReadError` |
| Non-ENOENT per-file `OSError` — e.g., `EIO`, `EACCES` | Raise `LogBackendReadError` |
| `sqlite3.DatabaseError` (the base class — covers `OperationalError`, which is how sqlite3 surfaces disk I/O errors) at the `execute()` of the SELECT **or** at connection setup (the `PRAGMA journal_mode=WAL` / schema-read on a corrupt `.db` file). Corruption surfaces at connect time for a genuinely corrupt file, so the reference impl wraps BOTH phases (`SQLiteLogBackend._get_conn_for_read()` + the per-call `execute`). A raw non-`DatabaseError` `OSError` is out of scope because sqlite3 reports I/O errors as `OperationalError` (a `DatabaseError` subclass), so the narrow `except sqlite3.DatabaseError` is sufficient. | Raise `LogBackendReadError` |
| SQLite `RuntimeError` from `_ensure_schema()` — BOTH the schema-version-mismatch case AND the defensive "schema_version row missing after INSERT OR IGNORE — db corruption suspected" case (a near-unreachable post-`INSERT OR IGNORE` branch) | Propagate uncaught — NOT a `sqlite3.DatabaseError`, so the narrow wrap does not catch it. The version-mismatch case is a config error; the corruption-suspected case is a defensive "impossible" branch whose practical reachability is near-zero (it would require an `INSERT OR IGNORE` to leave no row). A future PR MAY raise `sqlite3.DatabaseError` instead of `RuntimeError` at that branch so corruption surfaces as `LogBackendReadError` per this addendum's intent; doc-acknowledged here rather than wrapped, given reachability. |
| `psycopg.Error` (the base class) surviving the one-shot reconnect retry — corruption / I/O / connection drop after retry | Raise `LogBackendReadError`. The wrap catches `psycopg.Error` NARROWLY (mirroring SQLite's narrow `sqlite3.DatabaseError`), NOT bare `Exception`: a non-psycopg bug in the query builder is a code defect and propagates as itself, not relabeled a transient read failure. |
| `psycopg.Error` at **connection setup** — a *connectable-but-corrupt* database whose `_ensure_schema()` meta-table `SELECT` raises a raw `psycopg.Error` on first connect (note: `_ensure_schema`'s `except Exception: ... raise` re-raises this UNWRAPPED) | Raise `LogBackendReadError`. The reference impl wraps the pre-wrap connection setup in `PostgresLogBackend._get_conn_for_read()` (mirroring SQLite's `_get_conn_for_read()`), so connect-time-schema-read corruption gets the typed signal too — not just the per-call `execute()`. A pure connect *failure* still surfaces as `ValueError` (next row) because `_get_conn` itself converts `psycopg.connect` failures to a DSN-redacted `ValueError`; only a successful connect followed by a corrupt meta SELECT reaches this row. |
| Postgres `ValueError` (could-not-connect, DSN-redacted) / `RuntimeError` (schema-version mismatch) — at the FIRST connection setup (the pre-wrap `_get_conn_for_read()`) **or** on the inner one-shot-reconnect `_get_conn()` (server hard-down / schema mismatch on the reconnect target) | Propagate uncaught — config/deploy error, NOT a read failure. `_get_conn_for_read` catches only `psycopg.Error`, so these config-typed errors propagate on EVERY path (first-connect AND reconnect), not just first-connect. **Operator caveat:** a Postgres server restart / failover / idle-timeout mid-read can surface on the one-shot-reconnect path as this DSN-redacted `ValueError` (a connect failure, not a `psycopg.Error`). It therefore does NOT become `LogBackendReadError`; downstream cost consumers MUST fail closed on it via their broad backstop branch, not only on the typed signal (see `_costs._sum_via_backend`'s broad `except Exception`; the mandate spend-gate's parallel fail-closed posture is tracked in issue #506). |

The boundary resolves the apparent contradiction with the original Protocol
contract (`query()` MUST NOT raise for missing-backend state): absent/empty →
`[]` is the missing-backend contract; `LogBackendReadError` is reserved for
**unrecoverable I/O failures** where the backend exists and is entered but
cannot be read.

**Catch-breadth caveat (deliberate fail-closed-when-blind).** Both reference DB
backends catch a base class, so the read-failure class is intentionally broader
than "disk corruption only":

- SQLite's `except sqlite3.DatabaseError` also covers `OperationalError`, which
  includes transient `database is locked` contention under WAL with concurrent
  writers. Wrapping that into `LogBackendReadError` makes a downstream cost gate
  fail **closed** (block) on a lock a retry might clear. This is accepted under
  the project's fail-closed-when-blind posture (Principle 4: a blind spend gate
  MUST over-block) — a spurious refusal is recoverable; a silent budget overrun
  is not. The SQLite WAL-init path already retries `SQLITE_BUSY`/`SQLITE_LOCKED`
  per the queryable-backend contract MUST #4 above; per-`execute` lock
  contention is not retried and is classified as a (recoverable) read failure.
- Postgres's narrow `except psycopg.Error` deliberately classifies a
  statement-level `psycopg.ProgrammingError` (a genuine SQL-builder code defect)
  as a read failure rather than letting it surface as itself. This is the one
  place the "code defect surfaces as itself" rule yields to fail-closed: at the
  `_run_with_reconnect(_do)` call site there is no way to distinguish a builder
  bug from a server-rejected statement without inspecting `sqlstate`, so the
  conservative choice is fail-closed. Backends that want finer classification
  MAY inspect the error code and re-raise builder defects, but MUST NOT
  fail-open (treat the read as empty).

### The exception class

```python
class LogBackendReadError(AtomicAgentsError):
    """Raised by query() / tail() / aggregate() on unrecoverable read failure."""
```

Importable from both `atomic_agents` (top-level) and `atomic_agents.logs`
(backend-implementer surface):

```python
from atomic_agents import LogBackendReadError           # operator / caller surface
from atomic_agents.logs import LogBackendReadError      # backend implementer surface
```

### Scope

* `query()`, `tail()`, `aggregate()` **MUST** raise `LogBackendReadError` on
  unrecoverable read failures.
* `stats()` is **EXEMPT** — `stats()` is a racy diagnostic surface with a
  locked "MUST NOT use for control flow" contract (see §stats above). Wrapping
  its failures into `LogBackendReadError` would invite callers to use it for
  control decisions, which the locked contract explicitly refuses.
* `append()` and `delete_older_than()` are write/retention paths; this addendum
  does not constrain their exception surface.

### Cost-reader integration

`_costs._sum_via_backend()` uses a **layered catch**:

1. `except LogBackendReadError` — caught first; logs a clean "genuine
   unrecoverable read failure" message; returns
   `CostReadResult(total_usd=0.0, degraded=True)`.
2. `except Exception` — unconditional fail-closed backstop for non-conforming
   custom backends that raise other exception types; logs "unexpected exception
   type"; returns `CostReadResult(total_usd=0.0, degraded=True)`.

The two branches produce distinct log messages so operators can tell whether
degraded-mode came from a conforming backend (typed catch) or a non-conforming
backend (broad catch).

### Protocol isinstance note

`isinstance(obj, LogBackend)` checks structural method presence only (Python
`@runtime_checkable` Protocol — not behavior). Conformance to this MUST is
verified by the conformance test suite, not by the isinstance check.

### Conformance tests

The following tests verify this addendum. The three per-backend tests are in
`tests/test_log_protocol_conformance.py` (parametrized over every available
backend). There are **two** reader-seam tests: one in the conformance file (the
census's "+1") and one in `tests/test_costs.py` (outside the census):

* `test_query_raises_log_backend_read_error` — per-backend break_read injection,
  asserts `LogBackendReadError` from `query()`.
* `test_tail_raises_log_backend_read_error` — same for `tail()`.
* `test_aggregate_raises_log_backend_read_error` — same for `aggregate()`.
* `test_sum_via_backend_fails_closed_on_log_backend_read_error`
  (`tests/test_log_protocol_conformance.py`) — reader-seam test **in the
  conformance file**; this is the census's "+1 standalone reader-seam test"
  counted inside the 103 conformance term. Asserts `_sum_via_backend` returns
  `CostReadResult(degraded=True)` when the backend's `query()` raises
  `LogBackendReadError`.
* `test_backend_query_logbackendreaderror_returns_degraded`
  (`tests/test_costs.py`) — the near-identical reader-seam test in the
  cost-reader's own test file, **outside** this arc census; same assertion,
  keeps `test_costs.py` self-complete.

Filesystem break_read: replaces `log/` directory with a regular file so
`iterdir()` raises `NotADirectoryError` (OSError subclass) — exercises the
directory-level OSError path. Per-file ENOENT is intentionally NOT the
injection target (that returns `[]`, not an error).

SQLite break_read: corrupts the **real on-disk `.db` file** — drops the cached
thread-local connection, deletes the WAL/SHM sidecars, then overwrites the
main file with non-database garbage bytes. The next call builds a fresh
connection and surfaces a genuine `sqlite3.DatabaseError` ("file is not a
database") at connect/schema-setup time, which `_get_conn_for_read()` wraps
into `LogBackendReadError`. This exercises the realistic corruption path the
boundary table names (corruption surfaces at connection setup for a corrupt
file, not at the SELECT's `execute()`).

Postgres break_read: monkeypatches `_run_with_reconnect` to raise a real
`psycopg.OperationalError` (a `psycopg.Error` subclass) — exercises the
`try/except psycopg.Error` wrapping at the `_run_with_reconnect(_do)` call site,
proving the spec-named "psycopg error surviving the one-shot reconnect" boundary
row (NOT a generic-Exception catch-all; a non-psycopg error would propagate as a
code defect, matching SQLite's narrow catch). Monkeypatching avoids a live
server FOR THE BREAK INJECTION, but the postgres conformance test still requires
a **live Postgres** (`ATOMIC_AGENTS_TEST_POSTGRES_URL` + `psycopg`) to construct
the backend and append the seed record — it is SKIPPED otherwise (gated by
`_POSTGRES_AVAILABLE`).

**Assurance label — Postgres connect-time-schema-read row** (the boundary-table
row reading "`psycopg.Error` at **connection setup** — a *connectable-but-corrupt*
database whose `_ensure_schema()` meta-table `SELECT` raises a raw `psycopg.Error`
on first connect"; this is the row immediately *after* the "surviving the
one-shot reconnect retry" row — a textual anchor, since hand-maintained line
numbers drift on any edit).** The
behavioral conformance test injects at the **post-connect** `_run_with_reconnect`
seam, not at the **connect-time** `PostgresLogBackend._get_conn_for_read()` wrap
(the row where a connectable-but-corrupt database's `_ensure_schema` meta
`SELECT` raises a `psycopg.Error` on first connect). That connect-time wrap is
verified by **code-reading + structural mirroring of SQLite's
`_get_conn_for_read()`** — whose connect-time path *is* exercised by a real
on-disk-corruption behavioral test (above) — NOT by a dedicated live-Postgres
test. This matches the project's assurance-labeling convention for paths that
require an external system to exercise end-to-end (per
`feedback_verify_external_platform_claims`): the row is normatively specified and
structurally proven, with the live behavioral assertion deferred until a live
Postgres CI lane exists. A maintainer with a live Postgres can force the
connect-time path by monkeypatching `_ensure_schema` (or the meta `SELECT`) to
raise a `psycopg.Error` on a fresh thread-local connection and asserting
`query()` / `tail()` / `aggregate()` raise `LogBackendReadError` with the
`psycopg` error as `__cause__`.

---

### Versioned normative addendum — status='deduped' / status='in_flight' and idempotency audit fields (spec/45 PR2)

When agent.call() is invoked with an idempotency_key, the framework may short-circuit before the LLM runs. Two new status values and two new canonical fields record this, with these normative properties in all conforming LogBackends:

1. status='deduped' (a COMPLETED replay — call() returns the cached marker) and status='in_flight' (a concurrent twin holds the lease — call() raises DedupInFlight): on BOTH, the cost_usd key MUST be absent from the JSONL line (not 0.0 — omitted entirely). Cost readers MUST treat an absent cost_usd as zero, identical to lock_busy and skipped. No new logic in the cost path. Both records are written before call() returns/raises (no invisible exit path, Principle 5), mirroring the lock_busy record.
2. idempotency_key — NEW CANONICAL RunRecord field (str | None). Carries the caller-supplied key. MUST be recorded on every keyed run (ok, deduped, in_flight) so the replayed_run_id join is verifiable.
3. replayed_run_id — NEW CANONICAL RunRecord field (str | None). On status='deduped' records it carries the run_id of the original completed run whose result is served. Absent on ok and in_flight records (no result is served).
4. LogQuery.idempotency_key — conforming backends MUST support it as an AND-predicate returning only matching records; SQLite and Postgres backends MUST add a PARTIAL index, CREATE INDEX IF NOT EXISTS idx_idempotency_key ON run_records(idempotency_key) WHERE idempotency_key IS NOT NULL. The column is NULL for nearly every run (only keyed runs set it), so a partial index keeps the index small and the append hot-path cheap; the AND-predicate is an equality (`= ?`) lookup that matches the partial predicate, so it still resolves as an index seek.

Added OUTSIDE the 8-MUST count, following the versioned-addendum precedent of spec/09 §Cost-read error posture (#495) and spec/22 §Read-failure contract (#497).

---

### Versioned normative addendum — 'embed' primitive bucket and embed audit triggers (spec/46, issue #544 PR1)

The canonical primitive taxonomy (§"Canonical primitive taxonomy") is extended with one new bucket:

* `embed` — embedding reservation/release audit records, emitted by the agent.call() embed cost gate (spec/46, #544 PR1). Billing is ISOLATED from the chat `PRIMITIVE_HELPER` bucket because embedding uses `EMBEDDING_PRICING` (distinct from chat `PRICING`). Folding into `helper` or any existing bucket is irreversibly lossy once records are on disk — a `GROUP BY primitive` cost attribution query for embedding spend would be permanently ambiguous (Principle 5).

Four triggers map to `PRIMITIVE_EMBED = 'embed'` in `_PRIMITIVE_BY_TRIGGER`:

| Trigger | Emitted when |
|---------|-------------|
| `embed_batch_reservation` | Before the write_note() batch loop — worst-case cost reserved |
| `embed_batch_release` | In finally after the loop — actual cost recorded |
| `embed_reservation` | Before a single embed() call (query-embed gate at the CLI corpus-query site (#564), NOT inside agent.call(), shipped #544 PR2 in `_corpus_query`) |
| `embed_release` | In finally after a single embed() call (query-embed gate at the CLI corpus-query site (#564), NOT inside agent.call(), shipped #544 PR2 in `_corpus_query`) |

**Canonical shape for embed records (all four triggers):**

```
output_tokens: 0         (embedding is input-only; no output tokens)
cost_source: "actor"     (embedding spend is the agent's own spend)
cost_estimated: bool     (True when model_id not in EMBEDDING_PRICING)
batch_size: int          (for batch triggers; 1 for per-call triggers)
reserved_usd: float      (reservation records)
actual_usd: float        (release records; per-written-note byte-token estimate)
written_count: int       (embed_batch_release: notes successfully written)
cost_usd: ABSENT         (embed spend is audit-only this PR; NOT folded into the
                          cost_usd that sum_cost_for_period aggregates —
                          cross-call embed accounting deferred to #544 PR2)
```

> **SUPERSEDED for `cost_usd` by the #544 PR2a addendum below.** The `cost_usd: ABSENT`
> / "audit-only" line above describes the PR1 state. As of #544 PR2a a dedicated
> `embed_cost` record carries `cost_usd` (`cost_source: "actor"`) so embed spend now
> folds into `sum_cost_for_period` across calls. See the addendum for the live shape.

`cost_estimated=True` NEVER gates — it only affects the reserved amount. The fail-closed gate is `if CostReadResult.degraded AND effective_cap is not None`, not a function of cost_estimated.

`actual_usd` on a release record is a per-written-note ESTIMATE (the same UTF-8-byte token estimate as the reservation basis), summed over the notes successfully written. It is NOT conditioned on whether the underlying `embed()` returned `None`: `write_note()` returns a `NoteRef` and does not surface whether its internal `embed()` degraded to `None` (e.g. no API key — the note is still written via FTS and `write_note()` succeeds), so the orchestrator has no embed-None signal and charges the full per-note estimate. A true embed-None→`$0.0` accounting would require `write_note()` to report whether it embedded (an optional, capability-advertised return signal); that refinement is deferred to a focused follow-up (#589), not #544 PR2. The gate over-charges, never under-charges, on the no-key path, so the deferral is fail-safe.

The `embed_reservation` and `embed_release` triggers are emitted by the CLI corpus-query embed gate shipped in #544 PR2 (`_corpus_query` in `cli.py`). There is still no query-embed call site inside `agent.call()` (neither `memory.search()` nor `corpus.query()` is invoked by the orchestrator — confirmed by grep over `agent.py`); the gated path is the CLI corpus-query command (#564).

Added OUTSIDE the 8-MUST count, following the versioned-addendum precedent of spec/45 PR2 (#520) and spec/22 §Read-failure contract (#497).

### Versioned normative addendum — `embed_cost` cross-call accounting record (spec/46, issue #544 PR2a)

This addendum SUPERSEDES the PR1 statement above that "cost_usd: ABSENT (embed spend is audit-only this PR; ... cross-call embed accounting deferred to #544 PR2)". As of #544 PR2a, embed spend IS folded into the cost total that `sum_cost_for_period` aggregates across calls. The mechanism is a dedicated record, NOT a change to the reservation/release records.

A FIFTH trigger maps to `PRIMITIVE_EMBED = 'embed'` in `_PRIMITIVE_BY_TRIGGER`:

| Trigger | Emitted when |
|---------|-------------|
| `embed_cost` | In the same `finally` block IMMEDIATELY AFTER `embed_batch_release`, conditioned on `actual_usd > 0` — the cross-call accounting event |

The PR1 statement "Four triggers map to `PRIMITIVE_EMBED`" now reads "Five triggers" with `embed_cost` added.

**Canonical shape for the `embed_cost` record:**

```
trigger: "embed_cost"
parent_run_id: str       (the agent.call() run_id — audit-join anchor)
parent_agent: str        (the agent name — audit-join correctness, Principle 5)
model: str               (the resolved embedding model_id)
input_tokens: 0
output_tokens: 0
cost_usd: float          (== actual_usd; the ONLY embed record carrying cost_usd)
cost_source: "actor"     (the agent's own embedding spend)
cost_estimated: bool     (True when the per-note cost was byte-token estimated)
```

**The double-count invariant.** `embed_cost` is the ONLY embed trigger that carries `cost_usd`. `embed_batch_reservation` and `embed_batch_release` carry `reserved_usd` / `actual_usd` for reservation-reconciliation accounting but DO NOT carry `cost_usd`, so `sum_cost_for_period` (which sums `cost_usd` exclusively) folds embed spend in EXACTLY ONCE per batch. The `embed_batch_release` record remains the reservation-reconciliation anchor; `embed_cost` is the accounting event. This mirrors the helper/delegate pattern (leaf events carry `cost_usd`; release records carry accounting metadata).

`sum_cost_for_period(source='actor')` now includes prior embed spend on subsequent calls via the `embed_cost` records, so the embed cost gate's headroom baseline is accurate across calls (Principle 4).

Added OUTSIDE the 8-MUST count, same versioned-addendum precedent as the PR1 addendum above.

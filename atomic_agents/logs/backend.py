"""LogBackend Protocol — the contract every log implementation satisfies.

This is one of the open protocols in the protocol-pattern series
alongside MemoryBackend (#57, shipped), LLMBackend (#87, shipped),
JudgeBackend (#112, shipped), LockBackend (#60, shipped), and the
remaining Tier-2 backends PersonaBackend (#62), AgentProfileBackend
(#63), ToolRegistryBackend (#64), CorpusBackend (#65). Each Protocol
decouples one storage / dispatch axis so the framework's core stays
small and alternate implementations drop in without forking.

Issue #61 frames the urgency: today's ``agent.py:_log()`` writes JSONL
via ``_io.atomic_append_jsonl`` to ``<agent>/log/YYYY-MM/YYYY-MM-DD.jsonl``
and every dashboard reader walks those files directly. At single-agent
scale the artifact is perfect (grep-able, atomic append, ``rm -rf`` for
retention); at fleet scale the dashboard's month-walk becomes O(N) over
the full history, retention policies are manual rotation, and shipping
to Datadog/Loki/Cloud Logging requires forking the framework. The
LogBackend Protocol seals the layer so operators can plug
``SQLiteLogBackend`` / ``PostgresLogBackend`` / ``DatadogLogBackend``
without touching the call sites.

Scaffolding PR (#61 PR 1): the Protocol contract + canonical types +
``FilesystemLogBackend`` reference implementation. PR 2 wires the
backend into the 27+ ``self._log({...})`` sites in ``agent.py``,
``outcome._append_iteration_log``, and the four dashboard / cost-walker
readers; converts ``agent._log()`` to a thin wrapper that builds a
``RunRecord`` and calls ``self.log_backend.append(record)``. PR 3
ships ``SQLiteLogBackend`` as the canonical queryable backend
(stdlib-only; no optional extra needed). PR 4 locks
``docs/spec/22-log-backend.md`` and parameterizes the conformance suite
across both backends.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from .types import (
    LogAggregate,
    LogCapabilities,
    LogQuery,
    LogStats,
    RunRecord,
)


@runtime_checkable
class LogBackend(Protocol):
    """Contract every log backend implementation must satisfy.

    Implementations MUST NOT subclass this Protocol — it is structural.
    Implementations satisfy it by exposing the methods below with the
    documented behavior. The ``@runtime_checkable`` decorator enables
    ``isinstance(obj, LogBackend)`` to perform a method-presence check
    (not a signature check — signatures are static-typing's job).

    Scope is bound at backend construction. The agent instantiates one
    ``FilesystemLogBackend(agent_root)`` per agent, and uses it for the
    agent's full life. Unlike ``LockBackend`` (spec/21), there is no
    ``scope()`` method: logs scope by agent root only, with no
    sub-namespaces like dream/memory. Cross-primitive run records carry
    a ``primitive`` field instead.

    Append-only by default: ``append`` is the only mutation; ``query`` /
    ``tail`` / ``aggregate`` / ``stats`` are read-only; ``delete_older_than``
    is the single retention escape hatch and MAY be unimplemented when
    ``capabilities().supports_retention=False``.

    Note: ``isinstance(obj, LogBackend)`` checks structural method presence
    only (Python structural Protocol — not behavior). Conformance to the
    read-failure MUST (``query``/``tail``/``aggregate`` raise
    ``LogBackendReadError`` on unrecoverable I/O errors) is verified by
    the conformance test suite, not by this isinstance check. See spec/22
    §"spec/22 addendum — Read-failure posture".

    Ordering: records are stored and returned in ``ts`` order. ISO-8601
    timestamps with tz sort lexicographically into chronological order,
    so backends can use string comparison as the canonical sort key.
    """

    @property
    def backend_id(self) -> str:
        """Stable identifier — e.g., ``"filesystem"``, ``"sqlite"``,
        ``"datadog"``.

        Used by the registry for lookup (``get_log_backend(backend_id)``)
        and by diagnostic tooling that wants to log "which backend
        recorded this run?". Treat as a backwards-compatibility surface
        — operator deployments may pin against these strings.
        """
        ...

    def append(self, record: RunRecord) -> None:
        """Persist one ``RunRecord`` to the backend.

        Semantics:

        * **Atomic for records ≤ ``PIPE_BUF``** (POSIX, typically 4096
          bytes); SHOULD be atomic for larger records via backend-native
          serialization (SQL transaction, lease-token-checked Lua,
          single-request HTTP). The reference ``FilesystemLogBackend``
          inherits its bound from ``_io.atomic_append_jsonl`` which is
          atomic only for sub-``PIPE_BUF`` writes; records carrying
          rollup arrays (``helper_provenance``, ``delegations``,
          ``tool_calls``) routinely exceed this on the top-level
          ``agent.call()`` write. Operators with deployments that
          generate >4 KB records on shared NFS / multi-process hosts
          SHOULD select a transaction-backed backend (``SQLiteLogBackend``
          in PR 3, future Postgres/Datadog impls). Filesystem-default
          deployments on a single host accept the bound; the failure mode
          is silent partial-line append observable only via downstream
          ``json.JSONDecodeError`` skips in ``query()``.
        * MUST persist before returning. A crash immediately after
          ``append()`` returns MUST NOT lose the record. Filesystem
          backends fsync; SQL backends ack the commit; remote backends
          wait for server ack.
        * MUST preserve insertion order within the same agent run. Two
          ``append()`` calls in sequence MUST appear in that order from
          ``query()`` when sorted by ``ts``. (Records with identical
          ``ts`` are ordered by insertion.)
        * MUST NOT mutate the passed ``RunRecord``. ``RunRecord`` is
          a frozen dataclass; this is enforced structurally by Python,
          but backends MUST NOT copy-mutate either (e.g., adding fields
          to ``record.extra``).

        Idempotence: ``append`` is NOT idempotent on the input — calling
        ``append(record)`` twice MUST persist two records, not one. Log
        deduplication is the caller's concern (capture dedupes by
        ``(type, name, body hash)``; outcome iteration records dedupe
        by ``iteration``). The conformance suite pins this — see
        ``test_append_does_not_dedup``.

        Args:
            record: the ``RunRecord`` to persist. Required fields must
                be populated; optional fields are honored when set.
        """
        ...

    def query(self, filter: LogQuery) -> list[RunRecord]:
        """Return records matching ``filter`` in chronological order.

        Semantics:

        * Returns records in ``ts``-ascending order (oldest first).
          ISO-8601 lexicographic == chronological for tz-aware records.
        * Filters are AND-combined. ``LogQuery`` fields set to ``None``
          are not consulted; non-``None`` fields contribute equality
          predicates (with ``since/until`` contributing inclusive
          bounds and ``primitive=tuple(...)`` contributing membership).
        * Honors ``filter.limit`` AFTER sorting. A query for the
          earliest 100 records of the year returns the 100 oldest,
          not 100 unsorted records.
        * ``cost_source=None`` filter is special-cased for backward
          compatibility: legacy records without the field are treated
          as ``"actor"``. This mirrors the source-filter block in
          ``_costs.sum_cost_for_period``.
        * MUST return an empty list when the backend is empty; MUST
          NOT raise ``FileNotFoundError`` etc. for missing-backend
          state. PR 2 wires ``query`` into call paths that today
          tolerate missing log dirs gracefully (see
          ``dashboard/costs.py:128``).
        * MUST raise ``LogBackendReadError`` (from
          ``atomic_agents.exceptions``) for unrecoverable read failures
          (corruption, I/O error, lost database connection after all
          retries). Empty / absent backend state MUST NOT raise — return
          ``[]``. See spec/22 §"spec/22 addendum — Read-failure posture" for the
          full boundary definition.

        Performance: backends advertising
        ``capabilities().supports_aggregation_pushdown=True`` SHOULD
        push predicate evaluation to native query primitives
        (SQL ``WHERE``, Datadog facet filters). The reference
        ``FilesystemLogBackend`` walks month directories with a cheap
        date-window prefilter, then parses each line in-process.

        Args:
            filter: ``LogQuery`` describing which records to return.

        Returns:
            A new list of matching ``RunRecord`` instances in
            chronological order, truncated to ``filter.limit`` if set.
        """
        ...

    def tail(self, n: int) -> list[RunRecord]:
        """Return the most recent ``n`` records, chronological-LAST order.

        ``tail(3)`` returns three records with the newest at index 2
        (``result[-1]`` is the absolute newest record). This matches
        the Unix ``tail -n`` semantic and pairs naturally with
        ``records.sort(key=lambda r: r.ts)`` — the result is already
        sorted ascending.

        Edge cases (MUST hold):

        * ``tail(0)`` returns ``[]``.
        * ``tail(n)`` against a backend with fewer than ``n`` records
          returns all of them (not padded).
        * Empty backend returns ``[]`` (NOT raise).
        * Negative ``n`` raises ``ValueError`` (no implicit conversion).
        * Unrecoverable read failure raises ``LogBackendReadError``.
          See spec/22 §"spec/22 addendum — Read-failure posture".

        Performance: ``FilesystemLogBackend`` reverse-walks month dirs
        and day files to BOUND the scan to the most recent files;
        WITHIN a single day file the implementation reads the full
        content into memory (``reversed(f.readlines())``). For
        deployments where individual day files exceed ~10 MB,
        ``tail()`` materializes the latest day fully — prefer
        ``SQLiteLogBackend`` (PR 3) for sub-millisecond tail at scale.
        SQL backends use ``ORDER BY ts DESC LIMIT n`` then reverse
        client-side.

        Args:
            n: maximum number of records to return.

        Returns:
            A list of ``RunRecord`` instances, oldest first, newest last.
        """
        ...

    def aggregate(
        self,
        filter: LogQuery,
        agg: LogAggregate,
    ) -> dict[tuple, float | int]:
        """Compute a grouped aggregation over filter-matched records.

        Semantics:

        * Applies ``filter`` first to select records, then groups by
          the tuple of fields named in ``agg.group_by``, then computes
          the chosen ``agg.metric``.
        * The result is keyed by tuple-of-group-values. For
          ``group_by=("primitive",)`` the keys look like
          ``("agent_call",)``, ``("helper",)``; for
          ``group_by=("model", "status")`` the keys look like
          ``("claude-opus-4-7", "ok")``.
        * ``group_by=()`` returns a single entry keyed by ``()`` with
          the metric computed over all filter-matched records.
        * MUST raise ``ValueError`` when ``agg.metric`` is not in the
          canonical vocabulary (``types.VALID_METRICS``). Backends MUST
          NOT silently fall back to ``count``; surfacing the typo lets
          callers fail fast.
        * MUST raise ``LogBackendReadError`` on unrecoverable read
          failure (corruption, I/O error, lost connection). See spec/22
          §"spec/22 addendum — Read-failure posture" for the full boundary.
        * Backends advertising
          ``capabilities().supports_aggregation_pushdown=True`` SHOULD
          push the aggregation to native primitives. The reference
          ``FilesystemLogBackend`` aggregates in-memory after
          ``query()``.
        * **``group_by`` field names MUST resolve first against
          canonical ``RunRecord`` attributes, then fall through to
          ``record.extra`` for primitive-specific keys** (e.g.,
          ``"iteration"`` for outcome iteration records,
          ``"proposal_id"`` for judge records). Backends advertising
          ``supports_aggregation_pushdown=True`` MAY raise
          ``NotImplementedError`` for ``group_by`` fields that resolve
          only through ``extra`` (SQL ``GROUP BY`` over a JSON column
          requires native JSON extraction; not every backend supports
          this — operators wanting ``extra``-field group_bys with
          pushdown SHOULD use a backend with SQL JSON1 / equivalent).

        Metric semantics:

        * ``count`` — number of records in the group. ``int``.
        * ``sum_cost_usd`` — sum of ``cost_usd`` for records in the
          group; ``None`` cost_usd counted as 0.0. ``float``.
        * ``sum_input_tokens`` / ``sum_output_tokens`` — token-count
          sums. ``int``.
        * ``avg_latency_ms`` — mean of non-``None`` latencies in the
          group. ``float``. All-``None`` bucket returns ``None``
          (signaling "no latency observed" — NOT 0.0, which would
          look like "instant"); test 12 pins this.

        Args:
            filter: ``LogQuery`` for record selection.
            agg: ``LogAggregate`` specifying group fields and metric.

        Returns:
            ``dict`` mapping ``tuple`` of group-values to metric value.
        """
        ...

    def delete_older_than(self, threshold: datetime) -> int:
        """Delete records strictly older than ``threshold``. Return count.

        Semantics:

        * Records with ``ts < threshold`` (strict) are deleted. Records
          with ``ts == threshold`` survive.
        * MUST be idempotent: a second call with the same threshold
          deletes 0 records (because the first call removed them).
          The conformance suite pins this via
          ``test_delete_older_than_idempotent``.
        * MUST be atomic at the record level: a crash mid-deletion
          MUST NOT leave a half-deleted record. Filesystem backends
          rewrite the partial-day file via ``_io.atomic_write``.
        * MAY raise ``NotImplementedError`` when
          ``capabilities().supports_retention=False``. This is the
          escape hatch for append-only / immutable-store backends
          where retention is enforced externally (e.g., Datadog's
          log-retention policy is configured at the org level).

        Args:
            threshold: tz-aware ``datetime``. **Backends MUST raise
                ``ValueError`` on naive datetimes** — silent
                local-vs-UTC conversion is the failure shape that
                produces off-by-one-day retention errors near midnight.
                The conformance suite pins this contract.

        Returns:
            Count of records deleted.
        """
        ...

    def stats(self) -> LogStats:
        """Return a ``LogStats`` snapshot — DIAGNOSTIC use only.

        Racy by design: the returned counts MAY drift between this
        call and any subsequent action. Callers MUST NOT use
        ``stats()`` for control flow (e.g., "if
        ``stats().total_records > 1000`` then archive" — use
        ``query()`` with ``limit`` for that).

        Used by ``atomic-agents doctor`` and the dashboard's home tab
        to surface "how much history is here?". Backends MAY return
        coarse estimates (e.g., line counts rounded to nearest 1000)
        as long as the value is monotonic with appends.

        Performance note: the reference ``FilesystemLogBackend.stats()``
        opens every day file and counts lines — O(records). PR 2 wires
        this into the dashboard home tab; on agents with year-long
        history this is O(all-history) per page load. SQL backends
        push to ``COUNT(*)`` (O(1) with an index).

        Returns:
            ``LogStats`` with totals, date-range bounds, and (for
            disk-shaped backends) size in bytes.
        """
        ...

    def capabilities(self) -> LogCapabilities:
        """Backend capability declaration — see ``LogCapabilities``.

        Conformance tests assert claim-vs-behavior parity. Honest
        capabilities let callers fail fast against incompatible
        backends rather than discovering the mismatch mid-operation.
        """
        ...

"""FilesystemLogBackend — JSONL-on-disk reference implementation.

This is the default backend for single-host deployments. It wraps the
same on-disk shape ``agent.py:_log()`` has used since the framework's
first release: one JSONL line per record at
``<scope_root>/log/YYYY-MM/YYYY-MM-DD.jsonl``.

Two surface promises hold across PR 1 → PR 2:

1. **Byte-for-byte file preservation.** ``append()`` produces the exact
   line shape ``agent.py:3427`` writes today (via
   ``_io.atomic_append_jsonl``). External scripts that grep / tail the
   files keep working; the four dashboard / cost-walker readers
   (``dashboard/costs.py``, ``_costs.py``, ``dream.py``,
   ``dashboard/quality.py``) keep reading unchanged until PR 2 rewires
   them through ``query()``.

2. **Backward-compatible read.** ``query()`` reads the historical
   record shape (heterogeneous fields accumulated across arcs, including
   pre-``run_id`` records and pre-``cost_source`` records) without
   crashing. Permissive ``RunRecord.from_dict`` plus per-line
   ``json.JSONDecodeError`` skip semantics matches the legacy reader's
   defensive shape at ``_costs.py:142-148``.

Scope: bound at construction. ``FilesystemLogBackend(agent_root)``
operates on ``<agent_root>/log/``. There is no sub-scope concept —
unlike ``LockBackend`` which needs sub-namespaces for dream/memory,
logs scope by ``agent_root`` only with ``primitive`` distinguishing
record kinds within the same backend.

Append-only at the file level: appends never rewrite or relocate
existing lines; the only modification path is ``delete_older_than``,
which removes whole-file day records and atomically rewrites the
partial-cutoff day file via ``_io.atomic_write``.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .._io import atomic_append_jsonl, atomic_write
from .types import (
    LogAggregate,
    LogCapabilities,
    LogQuery,
    LogStats,
    METRIC_AVG_LATENCY_MS,
    METRIC_COUNT,
    METRIC_SUM_COST_USD,
    METRIC_SUM_INPUT_TOKENS,
    METRIC_SUM_OUTPUT_TOKENS,
    RunRecord,
    VALID_METRICS,
)


# ``YYYY-MM`` directories and ``YYYY-MM-DD.jsonl`` files. The strict
# regex is used to skip any stray files in the log tree (lockfiles,
# editor swap files) without raising — matches the legacy walker's
# defensive shape at ``_costs.py:131-134``.
_MONTH_DIR_RE = re.compile(r"^\d{4}-\d{2}$")
_DAY_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")


class FilesystemLogBackend:
    """JSONL-on-disk LogBackend — preserves the legacy on-disk shape.

    Conforms to the ``LogBackend`` Protocol. Constructed once per agent
    root; the ``scope_root`` is the directory under which the ``log/``
    subdirectory lives. PR 2 wires the agent's main log backend at
    ``AtomicAgent.__init__`` time, rooted at ``self.agent_root``.

    Append is per-line atomic via ``_io.atomic_append_jsonl`` (single
    POSIX append for typical <1KB lines). Read paths walk month dirs
    in ``YYYY-MM`` order; the ``ts`` ISO-8601 string is used as the
    canonical sort key (tz-aware ISO-8601 lexicographic ==
    chronological).

    Thread-safety: each method opens / writes / closes its own file
    handles inside its own call. Concurrent threads / processes
    appending to the same day file rely on POSIX's atomic-append-of-
    small-writes guarantee (same property ``_log()`` already depends
    on today). Concurrent ``delete_older_than`` against ``append`` is
    safe at the file-rename level (``atomic_write`` uses temp+rename),
    but the small race window where a record appended after the
    deletion-snapshot may still be removed if its ``ts`` falls under
    the cutoff is acceptable for retention semantics.

    Args:
        scope_root: directory containing (or that will contain) the
            ``log/`` subdirectory. Created on first ``append()``.
    """

    # ``backend_id`` is a ``@property`` (not a class attribute) for
    # parity with the LLM / Lock Protocol patterns. The property form
    # prevents instance-level mutation — ``b.backend_id = "spoof"``
    # would silently succeed against a class attribute and desynchronize
    # diagnostic logging from registry lookups.
    @property
    def backend_id(self) -> str:
        return "filesystem"

    def __init__(self, scope_root: Path) -> None:
        self._scope_root = Path(scope_root)

    @property
    def scope_root(self) -> Path:
        """The agent root this backend is bound to. Read-only after construction."""
        return self._scope_root

    @property
    def _log_dir(self) -> Path:
        """The ``<scope_root>/log/`` directory. May not exist yet."""
        return self._scope_root / "log"

    # ────────────────────────────────────────────────────────────
    # Append

    def append(self, record: RunRecord) -> None:
        """Append a record to ``<scope_root>/log/YYYY-MM/YYYY-MM-DD.jsonl``.

        Date selection uses ``record.ts`` when parseable, falling back
        to ``date.today()`` if blank or malformed. This matches today's
        ``agent.py:_log()`` shape, where the caller-provided record may
        have an explicit ``ts`` (set by ``self._log()``) and the date
        derives from that timestamp. Falling back to ``date.today()``
        on parse failure preserves the legacy "today's file" landing
        for malformed records rather than dropping them.

        **PR 2 wiring contract.** Today's ``agent.py:3425-3427`` shape
        sets both ``ts = datetime.now().astimezone().isoformat()`` AND
        computes the file path from ``date.today()`` — both local-tz.
        Because they're computed in the same call, ``ts.date() ==
        date.today()`` holds for the legacy path. PR 2's ``_log()``
        wrapper MUST preserve this invariant when building the
        ``RunRecord``: set ``ts`` via the same ``datetime.now()
        .astimezone().isoformat()`` idiom so the backend-derived date
        matches what ``date.today()`` would have produced. A wrapper
        that uses UTC ts (e.g., ``datetime.now(timezone.utc)``) on a
        non-UTC host will land records in a UTC-date file whereas the
        legacy path landed them in a local-date file — diverging the
        on-disk shape across the wiring transition.
        """
        day = _record_date(record)
        target = self._log_dir / day.strftime("%Y-%m") / f"{day.isoformat()}.jsonl"
        atomic_append_jsonl(target, json.dumps(record.to_dict()))

    # ────────────────────────────────────────────────────────────
    # Query

    def query(self, filter: LogQuery) -> list[RunRecord]:
        """Walk month dirs in range, parse, filter, sort, limit."""
        log_dir = self._log_dir
        if not log_dir.exists():
            return []

        since_str = filter.since.isoformat() if filter.since else None
        until_str = filter.until.isoformat() if filter.until else None
        since_date = filter.since.date() if filter.since else None
        until_date = filter.until.date() if filter.until else None

        # Normalize primitive filter into a frozenset of acceptable values
        # (None means no filter; string means single-value; tuple means
        # membership).
        primitive_filter: frozenset[str] | None
        if filter.primitive is None:
            primitive_filter = None
        elif isinstance(filter.primitive, str):
            primitive_filter = frozenset({filter.primitive})
        else:
            primitive_filter = frozenset(filter.primitive)

        results: list[RunRecord] = []

        for month_dir in sorted(log_dir.iterdir()):
            if not month_dir.is_dir() or not _MONTH_DIR_RE.match(month_dir.name):
                continue
            # Cheap month-window prefilter — skip month dirs whose
            # entire date range falls outside [since, until].
            if not _month_overlaps_window(month_dir.name, since_date, until_date):
                continue
            for day_file in sorted(month_dir.iterdir()):
                m = _DAY_FILE_RE.match(day_file.name)
                if not m:
                    continue
                # Day-level prefilter (skip whole days outside window).
                try:
                    day = date.fromisoformat(m.group(1))
                except ValueError:
                    continue
                if since_date and day < since_date:
                    continue
                if until_date and day > until_date:
                    continue
                # Read and parse.
                try:
                    text = day_file.read_text(encoding="utf-8")
                except OSError:
                    continue
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        # Skip malformed lines — matches legacy reader
                        # at ``_costs.py:147``. Surfacing these is the
                        # dashboard linter's job (#143 et al), not the
                        # backend's.
                        continue
                    record = RunRecord.from_dict(d)
                    if not _matches(
                        record, filter, since_str, until_str, primitive_filter
                    ):
                        continue
                    results.append(record)

        # Sort chronologically (ISO-8601 lexicographic == chronological
        # for tz-aware records). Stable sort preserves insertion order
        # for identical ``ts`` values.
        results.sort(key=lambda r: r.ts)

        if filter.limit is not None:
            results = results[: filter.limit]

        return results

    # ────────────────────────────────────────────────────────────
    # Tail

    def tail(self, n: int) -> list[RunRecord]:
        """Reverse-walk month dirs and day files; return last ``n`` chronologically."""
        if n < 0:
            raise ValueError(f"tail(n) requires n >= 0; got {n}")
        if n == 0:
            return []

        log_dir = self._log_dir
        if not log_dir.exists():
            return []

        collected: list[RunRecord] = []

        # Reverse-walk months (newest first), then reverse-walk day
        # files within each month, then reverse-walk lines within each
        # day file. Accumulate until we have n records; collected order
        # is newest-first.
        month_dirs = sorted(
            (
                p
                for p in log_dir.iterdir()
                if p.is_dir() and _MONTH_DIR_RE.match(p.name)
            ),
            reverse=True,
        )
        for month_dir in month_dirs:
            if len(collected) >= n:
                break
            day_files = sorted(
                (p for p in month_dir.iterdir() if _DAY_FILE_RE.match(p.name)),
                reverse=True,
            )
            for day_file in day_files:
                if len(collected) >= n:
                    break
                try:
                    text = day_file.read_text(encoding="utf-8")
                except OSError:
                    continue
                lines = [l.strip() for l in text.splitlines() if l.strip()]
                for line in reversed(lines):
                    if len(collected) >= n:
                        break
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    collected.append(RunRecord.from_dict(d))

        # Reverse to chronological (oldest first, newest last).
        collected.reverse()
        return collected

    # ────────────────────────────────────────────────────────────
    # Aggregate

    def aggregate(
        self,
        filter: LogQuery,
        agg: LogAggregate,
    ) -> dict[tuple, float | int]:
        """In-memory aggregation after ``query(filter)``.

        Filesystem advertises ``supports_aggregation_pushdown=False`` —
        callers see the cost transparently. SQL backends (PR 3) push
        this to ``GROUP BY`` natively.
        """
        if agg.metric not in VALID_METRICS:
            raise ValueError(
                f"Unknown aggregate metric: {agg.metric!r}. "
                f"Valid metrics: {sorted(VALID_METRICS)}"
            )

        records = self.query(filter)
        buckets: dict[tuple, list[RunRecord]] = defaultdict(list)
        for r in records:
            key = tuple(_get_record_field(r, name) for name in agg.group_by)
            buckets[key].append(r)

        result: dict[tuple, float | int] = {}
        for key, bucket in buckets.items():
            result[key] = _compute_metric(agg.metric, bucket)
        return result

    # ────────────────────────────────────────────────────────────
    # Retention

    def delete_older_than(self, threshold: datetime) -> int:
        """Drop whole-file days strictly before ``threshold``;
        rewrite the threshold day partially.

        Counts deleted records. Idempotent — calling again with the
        same threshold returns 0 because the prior call removed the
        candidates. Empty month dirs are cleaned at the end.

        Raises ``ValueError`` for naive datetimes (per the LogBackend
        contract). Silent local-vs-UTC conversion is the failure shape
        that produces off-by-one-day retention errors near midnight;
        operators MUST pass a tz-aware threshold.
        """
        if threshold.tzinfo is None:
            raise ValueError(
                "delete_older_than(threshold) requires a tz-aware "
                "datetime; naive datetime would silently convert "
                "local-vs-UTC and corrupt retention near midnight"
            )

        log_dir = self._log_dir
        if not log_dir.exists():
            return 0

        threshold_str = threshold.isoformat()
        threshold_date = threshold.date()

        deleted = 0

        for month_dir in sorted(log_dir.iterdir()):
            if not month_dir.is_dir() or not _MONTH_DIR_RE.match(month_dir.name):
                continue
            for day_file in sorted(month_dir.iterdir()):
                m = _DAY_FILE_RE.match(day_file.name)
                if not m:
                    continue
                try:
                    day = date.fromisoformat(m.group(1))
                except ValueError:
                    continue
                if day < threshold_date:
                    # Whole day before cutoff — drop it. Count first
                    # (need the line count for the return value).
                    try:
                        text = day_file.read_text(encoding="utf-8")
                    except OSError:
                        text = ""
                    deleted += sum(1 for line in text.splitlines() if line.strip())
                    try:
                        day_file.unlink()
                    except OSError:
                        continue
                elif day == threshold_date:
                    # Partial day — rewrite atomically with only the
                    # records whose ``ts >= threshold``.
                    try:
                        text = day_file.read_text(encoding="utf-8")
                    except OSError:
                        continue
                    keep_lines: list[str] = []
                    for line in text.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                        except json.JSONDecodeError:
                            # Preserve unparseable lines — the linter's
                            # job to surface them, not the retention
                            # path's job to silently drop them.
                            keep_lines.append(line)
                            continue
                        rec_ts = str(d.get("ts", ""))
                        if rec_ts < threshold_str:
                            deleted += 1
                            continue
                        keep_lines.append(line)
                    if keep_lines:
                        atomic_write(day_file, "\n".join(keep_lines) + "\n")
                    else:
                        try:
                            day_file.unlink()
                        except OSError:
                            pass
                # day > threshold_date: leave alone (future records).

            # Clean up empty month dirs.
            try:
                if not any(month_dir.iterdir()):
                    month_dir.rmdir()
            except OSError:
                # Race or non-empty — leave alone.
                pass

        return deleted

    # ────────────────────────────────────────────────────────────
    # Stats

    def stats(self) -> LogStats:
        """Compute ``LogStats`` by walking files (line-count + size)."""
        log_dir = self._log_dir
        if not log_dir.exists():
            return LogStats(
                total_records=0,
                oldest_ts=None,
                newest_ts=None,
                size_bytes=None,
                records_today=0,
                records_this_month=0,
            )

        total_records = 0
        size_bytes = 0
        oldest_ts: str | None = None
        newest_ts: str | None = None
        today = date.today()
        records_today = 0
        records_this_month = 0
        today_iso = today.isoformat()
        this_month = today.strftime("%Y-%m")

        for month_dir in sorted(log_dir.iterdir()):
            if not month_dir.is_dir() or not _MONTH_DIR_RE.match(month_dir.name):
                continue
            for day_file in sorted(month_dir.iterdir()):
                m = _DAY_FILE_RE.match(day_file.name)
                if not m:
                    continue
                try:
                    size_bytes += day_file.stat().st_size
                except OSError:
                    continue
                day_str = m.group(1)
                try:
                    text = day_file.read_text(encoding="utf-8")
                except OSError:
                    continue
                lines = [l.strip() for l in text.splitlines() if l.strip()]
                day_count = len(lines)
                total_records += day_count
                if month_dir.name == this_month:
                    records_this_month += day_count
                if day_str == today_iso:
                    records_today += day_count
                if lines:
                    # First/last lines give oldest/newest ts.
                    first_ts = _safe_ts_of_line(lines[0])
                    last_ts = _safe_ts_of_line(lines[-1])
                    if first_ts:
                        if oldest_ts is None or first_ts < oldest_ts:
                            oldest_ts = first_ts
                    if last_ts:
                        if newest_ts is None or last_ts > newest_ts:
                            newest_ts = last_ts

        return LogStats(
            total_records=total_records,
            oldest_ts=oldest_ts,
            newest_ts=newest_ts,
            size_bytes=size_bytes,
            records_today=records_today,
            records_this_month=records_this_month,
        )

    # ────────────────────────────────────────────────────────────
    # Capabilities

    def capabilities(self) -> LogCapabilities:
        return LogCapabilities(
            supports_aggregation_pushdown=False,
            supports_streaming=False,
            supports_retention=True,
            durable=True,
            supports_canonical_export=True,  # spec/40 addendum
        )

    def export(self, query=None):
        """Export log records as a LogExport canonical object (spec/40).

        Args:
            query: ``LogExportQuery | None``. Pass None for all records.

        Returns:
            ``LogExport`` with (RunRecord, raw_bytes) tuples.
        """
        from ..export.filesystem import export_log
        from ..export.types import LogExportQuery

        if query is None:
            query = LogExportQuery()
        return export_log(self, query)

    def export_all(self):
        """Convenience wrapper — unbounded export. Equivalent to export(None).

        WARNING: Materializes ALL log records in memory. For large log histories
        use export(LogExportQuery(log_query=LogQuery(since=...))) instead.
        """
        return self.export(None)


# ────────────────────────────────────────────────────────────────────
# Module-level helpers


def _record_date(record: RunRecord) -> date:
    """Date for a record's day file. Falls back to ``date.today()`` on parse failure."""
    ts = record.ts
    if ts:
        try:
            return datetime.fromisoformat(ts).date()
        except ValueError:
            pass
    return date.today()


def _month_overlaps_window(
    month_name: str,
    since: date | None,
    until: date | None,
) -> bool:
    """Whether a ``YYYY-MM`` month overlaps the optional date window."""
    try:
        year, month = (int(x) for x in month_name.split("-"))
    except ValueError:
        return False
    month_start = date(year, month, 1)
    if month == 12:
        month_end = date(year + 1, 1, 1)
    else:
        month_end = date(year, month + 1, 1)
    # month_end is exclusive
    if since and month_end <= since:
        return False
    if until and month_start > until:
        return False
    return True


def _matches(
    record: RunRecord,
    filter: LogQuery,
    since_str: str | None,
    until_str: str | None,
    primitive_filter: frozenset[str] | None,
) -> bool:
    """Apply AND-filter predicates to a record."""
    if filter.run_id is not None and record.run_id != filter.run_id:
        return False
    if primitive_filter is not None and record.primitive not in primitive_filter:
        return False
    if filter.status is not None and record.status != filter.status:
        return False
    if filter.model is not None and record.model != filter.model:
        return False
    if filter.cost_source is not None:
        # Legacy records without cost_source count as "actor" — mirrors
        # ``_costs.sum_cost_for_period`` line 149-157.
        rec_source = record.cost_source if record.cost_source is not None else "actor"
        if filter.cost_source == "actor":
            if rec_source != "actor":
                return False
        else:
            if rec_source != filter.cost_source:
                return False
    if filter.mandate_id is not None and record.mandate_id != filter.mandate_id:
        return False
    if (
        filter.parent_run_id is not None
        and record.parent_run_id != filter.parent_run_id
    ):
        return False
    if filter.agent_name is not None:
        # Lenient: match when record.agent_name equals filter OR is None.
        # Pre-PR-2 on-disk records don't carry agent_name; under
        # filesystem's per-agent-dir scoping, every record in the dir
        # IS the named agent's. The filter is load-bearing only for
        # shared-backend deployments (SQLite/Postgres shared file),
        # where post-PR-2 records all carry the field. Lenient matching
        # preserves backward compat for filesystem reads without
        # weakening the shared-backend cross-agent isolation property.
        if record.agent_name is not None and record.agent_name != filter.agent_name:
            return False
    if since_str is not None and record.ts < since_str:
        return False
    if until_str is not None and record.ts > until_str:
        return False
    return True


def _get_record_field(record: RunRecord, name: str) -> Any:
    """Return the value of a RunRecord field, falling through to ``extra``."""
    # __dataclass_fields__ is the source of truth for canonical fields.
    if name in record.__dataclass_fields__:
        return getattr(record, name)
    # Fall through to ``extra`` for primitive-specific group-by keys
    # (e.g., aggregating by ``iteration`` or ``proposal_id``).
    return record.extra.get(name)


def _compute_metric(metric: str, bucket: list[RunRecord]) -> float | int:
    """Compute one of the canonical metrics over a bucket of records."""
    if metric == METRIC_COUNT:
        return len(bucket)
    if metric == METRIC_SUM_COST_USD:
        return float(sum((r.cost_usd or 0.0) for r in bucket))
    if metric == METRIC_SUM_INPUT_TOKENS:
        return int(sum((r.input_tokens or 0) for r in bucket))
    if metric == METRIC_SUM_OUTPUT_TOKENS:
        return int(sum((r.output_tokens or 0) for r in bucket))
    if metric == METRIC_AVG_LATENCY_MS:
        latencies = [r.latency_ms for r in bucket if r.latency_ms is not None]
        if not latencies:
            # All-None bucket — return None (the dict value will be
            # None, signaling "no latency observed"; not 0.0 which
            # would look like "instant").
            return None  # type: ignore[return-value]
        return float(sum(latencies) / len(latencies))
    # Unreachable — VALID_METRICS gate in aggregate() catches unknowns.
    raise ValueError(f"unreachable: unknown metric {metric!r}")


def _safe_ts_of_line(line: str) -> str | None:
    """Extract ``ts`` from a JSONL line without crashing on malformed input."""
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return None
    ts = d.get("ts")
    return str(ts) if ts else None

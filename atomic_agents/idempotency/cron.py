"""cron_tick_key — deterministic idempotency key helper for cron triggers (spec/45).

A pure stdlib function; no I/O, no agent deps. Safe to import without pulling
in the LLM stack. The absence of AtomicAgent import is intentional (circular-
import safety — idempotency/ must not import from agent.py).

Usage in a cron agent script::

    from atomic_agents.idempotency import cron_tick_key
    from datetime import datetime, timezone

    key = cron_tick_key("my-agent", "daily-digest", datetime.now(timezone.utc), "day")
    response = agent.call(work_item="...", idempotency_key=key)

The same ``key`` is produced for any ``when`` that falls within the same
granularity bucket (same hour, day, week). Different buckets produce different
keys — ensuring one execution per bucket regardless of how many times the cron
fires within that window.
"""

from __future__ import annotations

from datetime import datetime, timezone

# Granularity → seconds in the bucket (for floor-division).
_GRANULARITY_SECONDS: dict[str, int] = {
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "week": 604800,
}


def cron_tick_key(
    agent_name: str,
    schedule_name: str,
    when: datetime,
    granularity: str,
) -> str:
    """Return a stable idempotency key for a cron schedule tick (spec/45 MUST 14).

    Floors ``when`` to the schedule bucket defined by ``granularity`` and
    formats the result deterministically as
    ``<agent_name>:<schedule_name>:<bucket_ts>`` where ``bucket_ts`` is a
    Unix epoch integer (seconds) for the floor of the bucket.

    Two ``when`` values within the same granularity window produce the same
    key; two ``when`` values in adjacent windows produce different keys. The
    timestamp is converted to UTC before flooring so the key is timezone-
    invariant (two calls from different time zones within the same UTC window
    produce the same key).

    The returned key passes ``_validate_key`` without ``PathTraversalError``:
    colons (``:``) are valid in idempotency keys (not path separators);
    no NUL bytes or control characters are introduced; the maximum length
    depends on ``agent_name`` + ``schedule_name`` lengths but is always far
    below the 2048-char backend limit for any reasonable names.

    Args:
        agent_name: the agent's name (e.g. ``"daily-digest"``). MUST NOT
            contain a colon (``:``), a path separator (``/`` or ``\\``), or a
            control character — these are REJECTED with ``ValueError`` because
            the key is colon-delimited: an un-rejected colon would make
            ``("a:b", "c")`` and ``("a", "b:c")`` collide into the SAME key, a
            false-dedup that silently drops a real run. Agent names follow the
            same naming rules as agent folder names.
        schedule_name: a human-readable label for this schedule
            (e.g. ``"morning-run"``). Used to distinguish multiple cron
            schedules for the same agent. Subject to the same colon / path-
            separator / control-character rejection as ``agent_name``.
        when: the moment the cron tick fired. MUST be a timezone-aware
            datetime (a naive datetime raises ValueError — see Raises).
            Converted to UTC before flooring so keys are timezone-invariant.
        granularity: one of ``"minute"``, ``"hour"``, ``"day"``, ``"week"``.
            Controls the size of the bucket (and thus how many retries within
            that window are considered the same tick). NOTE: ``"week"`` buckets
            are epoch-anchored (the Unix epoch 1970-01-01 is a Thursday), so a
            week runs Thursday→Wednesday, NOT a calendar (Mon/Sun) week. Dedup
            is unaffected (same week collides, adjacent weeks differ); operators
            needing calendar-week alignment should compute their own bucket key.

    Returns:
        A stable idempotency key string of the form
        ``<agent_name>:<schedule_name>:<bucket_epoch_seconds>``.

    Raises:
        ValueError: when ``granularity`` is not one of the four supported
            values (``"minute"``, ``"hour"``, ``"day"``, ``"week"``); OR when
            ``when`` is a naive datetime (no tzinfo) — naive inputs would make
            the key depend on the host's local timezone, defeating dedup; OR
            when ``agent_name`` or ``schedule_name`` contains a colon (``:``),
            a path separator (``/`` or ``\\``), or a control character — these
            would corrupt the colon-delimited key (a colon makes ``("a:b","c")``
            and ``("a","b:c")`` collide into one false-dedup bucket).

    Examples:
        Two times within the same hour produce the same key::

            t1 = datetime(2026, 6, 16, 10, 0, 0, tzinfo=timezone.utc)
            t2 = datetime(2026, 6, 16, 10, 45, 0, tzinfo=timezone.utc)
            assert cron_tick_key("agent", "sched", t1, "hour") == \\
                   cron_tick_key("agent", "sched", t2, "hour")

        The next hour produces a different key::

            t3 = datetime(2026, 6, 16, 11, 0, 0, tzinfo=timezone.utc)
            assert cron_tick_key("agent", "sched", t1, "hour") != \\
                   cron_tick_key("agent", "sched", t3, "hour")
    """
    # Reject delimiter-colliding components LOUDLY. The key is colon-delimited
    # (``agent:schedule:bucket``); a colon in either component would let
    # ("a:b", "c") and ("a", "b:c") produce the SAME key — a false-dedup that
    # silently drops a real run (strictly worse than no dedup). Path separators
    # and control characters are rejected for the same reason a backend key is
    # (defense-in-depth; the key flows into _validate_key downstream too).
    for _label, _value in (
        ("agent_name", agent_name),
        ("schedule_name", schedule_name),
    ):
        if ":" in _value or "/" in _value or "\\" in _value:
            raise ValueError(
                f"cron_tick_key: {_label}={_value!r} must not contain ':' or a "
                "path separator ('/' or '\\\\') — the key is colon-delimited and "
                "an embedded delimiter would collide distinct (agent, schedule) "
                "pairs into one false-dedup bucket."
            )
        if any(ord(_ch) < 32 for _ch in _value):
            raise ValueError(
                f"cron_tick_key: {_label}={_value!r} must not contain control "
                "characters (ord < 32)."
            )
    if granularity not in _GRANULARITY_SECONDS:
        valid = sorted(_GRANULARITY_SECONDS.keys())
        raise ValueError(
            f"cron_tick_key: unsupported granularity={granularity!r}. "
            f"Must be one of {valid}."
        )
    # Reject NAIVE datetimes loudly. astimezone() on a naive datetime assumes
    # the host's LOCAL timezone, which would make the bucket key host-tz-dependent
    # (and DST-dependent) — silently breaking the timezone-invariance guarantee
    # this helper exists to provide. Cron scripts that use datetime.now() (naive)
    # must pass datetime.now(timezone.utc) instead. spec/45 MUST 14. Mirrors the
    # loud-caller-bug posture used for invalid keys (PathTraversalError).
    if when.tzinfo is None or when.tzinfo.utcoffset(when) is None:
        raise ValueError(
            "cron_tick_key: 'when' must be a timezone-aware datetime (got naive). "
            "Pass datetime.now(timezone.utc) — a naive datetime would make the "
            "key depend on the host's local timezone, breaking dedup across hosts."
        )
    bucket_seconds = _GRANULARITY_SECONDS[granularity]
    # Convert to UTC timestamp, then floor to the granularity bucket via
    # integer division. This is timezone-invariant: the same wall-clock moment
    # expressed in different time zones produces the same UTC epoch integer.
    utc_when = when.astimezone(timezone.utc)
    epoch = int(utc_when.timestamp())
    bucket_epoch = (epoch // bucket_seconds) * bucket_seconds
    return f"{agent_name}:{schedule_name}:{bucket_epoch}"

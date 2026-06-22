"""Cost calculation + multi-tier guardrails per spec/09-cost-observability.

Pricing table is hardcoded; update when Anthropic/OpenAI/Moonshot change rates.
"""

from __future__ import annotations
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .exceptions import LogBackendReadError

if TYPE_CHECKING:
    from .logs import LogBackend

logger = logging.getLogger(__name__)

# Cost-event source categories (spec/28 actor/judge split + spec/30 audit).
# Legacy records (no cost_source field) are treated as "actor" on read.
CostSource = Literal["actor", "judge", "audit"]


@dataclass
class CostReadResult:
    """Internal result from the cost-summing reader.

    Returned by sum_cost_for_period and _sum_via_backend. NOT part of the
    public API — internal to _costs.py. Do not export from atomic_agents/__init__.py.

    total_usd:      summed cost for the requested period.
    degraded:       True when the read was partial or completely blind.
                    Gate sites must map degraded=True → fail-closed (treat as
                    over-cap). Reporting consumers that adopt this reader (none
                    today; the dashboard uses the spec/22 query() path instead)
                    should surface total_usd as possibly-incomplete rather than
                    crash.
    dropped_records: count of per-line corruption events skipped (unparseable
                    JSON / non-numeric / boolean cost_usd). Non-zero via
                    below-threshold per-line skips, via the current-day >50%
                    fail-closed return, AND via a historical >50%-corrupt file
                    that is skipped while the rest of the month keeps summing.
                    0 on a whole-file/backend blind failure (OSError, unreadable
                    dir, backend exception) and on an empty/whitespace file
                    (treated as no-cost, NOT corruption) — any path that produced
                    no line-level tally. Diagnostic only — `degraded` is the
                    load-bearing flag (a whole-file blind skip flips degraded=True
                    with dropped_records=0).
    """

    total_usd: float
    degraded: bool
    dropped_records: int


# USD per 1M tokens — input / output
PRICING: dict[str, dict[str, float]] = {
    # Anthropic
    "claude-opus-4-8": {"input": 5.0, "output": 25.0},  # current Opus-tier default
    "claude-opus-4-7-20260101": {"input": 5.0, "output": 25.0},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0},  # alias
    "claude-sonnet-4-6-20260101": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.0},
    "claude-haiku-4-5": {"input": 0.80, "output": 4.0},
    # OpenAI (placeholder rates; update when published)
    "gpt-5": {"input": 5.0, "output": 20.0},
    "gpt-5-mini": {"input": 0.50, "output": 2.0},
    "gpt-5-nano": {"input": 0.10, "output": 0.50},
    # Moonshot (placeholder rates — verify against current Moonshot pricing).
    # Both the api.moonshot.ai (dot-style) and api.moonshot.cn (dash-date-style)
    # endpoints expose distinct model identifiers; cost lookup needs entries
    # for whichever an operator selects via `--model`.
    "moonshot/moonshot-v1-128k": {"input": 0.30, "output": 1.20},
    "moonshot/moonshot-v1-32k": {"input": 0.30, "output": 1.20},
    "moonshot/moonshot-v1-8k": {"input": 0.30, "output": 1.20},
    "moonshot/kimi-k2.6": {"input": 0.30, "output": 1.20},  # thinking; .ai
    "moonshot/kimi-k2.5": {"input": 0.30, "output": 1.20},  # thinking; .ai
    "moonshot/kimi-k2-0905-preview": {"input": 0.30, "output": 1.20},  # thinking; .cn
    "moonshot/kimi-k2-0711-preview": {"input": 0.30, "output": 1.20},  # thinking; .cn
    "moonshot/kimi-2.6": {"input": 0.30, "output": 1.20},
    # Vertex AI — Gemini family (via google-genai SDK with vertexai=True).
    # IMPORTANT: Keys carry the full ``vertex/`` prefix — matching exactly what
    # operators write in model.md.  calc_cost() receives the model string as-is
    # from call() (which passes the prefixed form), so keys MUST match.
    # Rates from Vertex AI pricing page (verified 2026-06-09).  Vertex pricing
    # may differ from direct Gemini API pricing; update when Vertex publishes
    # changes.
    # NOTE: Unknown vertex/* models (not listed below) fall through to
    # _fallback_pricing(), which uses the GLOBAL max input/output rates across
    # PRICING (currently claude-opus at 5/25) — above any Vertex rate. So an
    # unpriced Vertex model is costed pessimistically, not cheaply. These entries
    # do NOT change the fallback sentinel (all are below the existing opus max);
    # operators wanting accurate cost on a new Vertex model should add an explicit
    # entry rather than relying on the opus-rate fallback.
    "vertex/gemini-2.5-flash": {"input": 0.30, "output": 2.50},  # $0.30/$2.50 per 1M
    "vertex/gemini-2.5-pro": {"input": 1.25, "output": 10.00},  # $1.25/$10.00 per 1M
    "vertex/gemini-2.0-flash": {"input": 0.10, "output": 0.40},  # $0.10/$0.40 per 1M
    "vertex/gemini-2.0-flash-lite": {
        "input": 0.075,
        "output": 0.30,
    },  # $0.075/$0.30 per 1M
}

# ──────────────────────────────────────────────────────────────────────────────
# Embedding pricing — SEPARATE from PRICING (chat models).
#
# CRITICAL ISOLATION: _embedding_fallback_rate() scans ONLY this dict.
# calc_embedding_cost() NEVER calls _fallback_pricing() (the chat-model
# version). Embedding rates are input-only (no output column) and three to
# five orders of magnitude smaller than chat rates. Merging the tables would
# cause an unknown embedding model to fall back to the Opus output rate ($25/1M
# instead of $0.13/1M) -- a ~192x overcount (25 / 0.13) that would spuriously block all
# embedding calls via the spec/46 reservation mandate.
#
# Source verification (Principle #12 applied outward per MEMORY.md):
# Rates verified 2026-06-17 against the authoritative OpenAI per-model docs
# (each model's own docs page on developers.openai.com):
#   https://developers.openai.com/api/docs/models/text-embedding-3-small  -> $0.02/1M
#   https://developers.openai.com/api/docs/models/text-embedding-3-large  -> $0.13/1M
#   https://developers.openai.com/api/docs/models/text-embedding-ada-002  -> $0.10/1M
# All three confirmed directly on the authoritative pages (NOT community-only);
# also consistent with the pages-per-dollar derivation below.
# Pages-per-dollar derivation (developers.openai.com embeddings guide):
#   3-small 62,500 pp/$ × 800 tok/page = 50M tok/$ → $0.020/1M;
#   3-large 9,615 pp/$ × 800 tok/page = 7.69M tok/$ → $0.130/1M;
#   ada-002 12,500 pp/$ × 800 tok/page = 10M tok/$ → $0.100/1M.
# NOTE on the $0.065/1M figure seen elsewhere for text-embedding-3-large:
# this was a historical/erroneous pricing-page entry for the SYNCHRONOUS rate
# that has since been CORRECTED to $0.130/1M. It is NOT a Batch-API discount
# rate: the authoritative per-model docs page (verified 2026-06-17) shows
# text-embedding-3-large's standard AND Batch API price are BOTH $0.130/1M.
# The $0.130/1M rate below is authoritative (per-model docs page, confirmed by
# the pages-per-dollar derivation above). Disregard any $0.065/1M figure.
#
# All rates are USD per 1M INPUT tokens (embeddings are input-only; there is
# no output token column for embedding models).

EMBEDDING_PRICING: dict[str, float] = {
    # USD per 1M input tokens (input-only; no output column for embedding models).
    # Each rate verified against its authoritative OpenAI per-model docs page,
    # accessed 2026-06-17 (Principle #12 applied outward).
    # source: https://developers.openai.com/api/docs/models/text-embedding-3-small (Cost $0.02)
    "text-embedding-3-small": 0.020,  # $0.020/1M tokens
    # source: https://developers.openai.com/api/docs/models/text-embedding-3-large (Cost $0.13)
    "text-embedding-3-large": 0.130,  # $0.130/1M tokens; see discrepancy note above
    # source: https://developers.openai.com/api/docs/models/text-embedding-ada-002 (Cost $0.10)
    "text-embedding-ada-002": 0.100,  # $0.100/1M tokens (legacy, still supported)
}

# Bytes-per-token constant for the UTF-8-based embed token estimate.
# BPE tokens are bounded by UTF-8 byte length; bytes/3 is conservative for
# natural-language text (covers CJK/emoji where code-point count under-counts
# ~3x). Single source of truth shared by agent.py and cli.py — eliminates
# drift if the constant is tuned in future.
EMBED_BYTES_PER_TOKEN: int = 3

# Per-process dedup set for unknown embedding model warnings.
_unknown_embedding_model_warned: set[str] = set()

# Cap for per-process dedup-warning sets: beyond this many distinct keys, always
# warn (no suppression) rather than growing a set without bound. Shared by the
# embedding unknown-model set and the cost-corruption set so the two cannot drift.
_WARNED_SET_CAP = 1000

# Upper bound on a single embedding call's input_tokens. Real inputs are <= the
# model max (~8192 tokens/item). A count far above this is a caller bug; capping
# keeps the float cost math (input_tokens * rate) from overflowing to inf -- an
# inf reservation would make the PR3 cap gate block EVERY embedding call (a
# self-DoS via one bad token count).
_MAX_EMBEDDING_INPUT_TOKENS = 100_000_000


def _embedding_fallback_rate() -> float:
    """Return the maximum known embedding input rate from EMBEDDING_PRICING.

    Used when a model id is not in EMBEDDING_PRICING so unknown models are
    over-counted (pessimistic) rather than silently treated as free.

    CRITICAL: this function scans ONLY EMBEDDING_PRICING -- it NEVER reads
    from PRICING (chat models). Merging the tables would make an unknown
    embedding model fall back to the Opus output rate (~$75/1M) rather than
    the correct embedding ceiling (~$0.13/1M). See the EMBEDDING_PRICING
    docblock above for the isolation rationale.
    """
    return max(EMBEDDING_PRICING.values())


def calc_embedding_cost(model_id: str, input_tokens: int) -> tuple[float, bool]:
    """Compute USD cost for one embedding call (input-only; no output tokens).

    Returns ``(cost_usd, cost_estimated)`` matching the shape of ``calc_cost()``:
    - ``cost_usd``: USD cost rounded UP to 6 decimal places. Rounding up (not
      ``round()``) is deliberate: this is the worst-case RESERVATION primitive
      used by both live gate sites, and ``round()`` floors any sub-$0.000001
      call to 0.0 (e.g. a 3-small call under ~25 tokens), so a high-volume
      per-text reservation loop would systematically under-reserve to 0.
      Ceiling keeps every non-empty reservation strictly positive (Principle #4
      -- no path under-reserves its cost guardrail). The over-count is at most
      $0.000001 per call.
    - ``cost_estimated``: True when ``model_id`` was not in ``EMBEDDING_PRICING``
      and the cost used the fallback (max known embedding rate). False when the
      model was priced exactly.

    The caller distinction between 'estimated' and 'degraded' matters for both
    wired gate sites (spec/46 MANDATE):
    - ``cost_estimated=True`` means "unpriced model, used max known rate" --
      the cost is pessimistic but usable. Fail-close only when a cap exists.
    - A CostReadResult(degraded=True) (from sum_cost_for_period) means "I/O
      failure reading cost history" -- a different signal entirely.
    Do NOT conflate these; see MEMORY.md lesson "Fail-closed only where there's
    something to protect" and the spec/46 MANDATE wording.

    This function is the worst-case reservation primitive wired into BOTH live
    gate sites: the agent.call() capture-commit batch gate (#544 PR1,
    embed_batch_reservation / embed_batch_release) and the CLI corpus-query gate
    (#544 PR2, embed_reservation / embed_release JSONL audit records).

    Negative ``input_tokens`` are clamped to 0: this helper is the worst-case
    RESERVATION primitive, and a negative reservation would REDUCE
    the reserved amount (a guardrail-escape shape per Principle #4 -- no path
    escapes its cost guardrail). A bad token count must never lower the
    reservation below zero.
    """
    if isinstance(input_tokens, bool) or not isinstance(input_tokens, int):
        # Non-int (NaN/inf floats, None, bool, str) is a caller bug. A NaN
        # reservation compares false against any cap (fail-OPEN); an inf blocks
        # every call (fail-CLOSED-for-all). Neither is a valid reservation, so
        # treat as 0 with a loud warning rather than poison the PR3 gate.
        logger.warning(
            "calc_embedding_cost received non-integer input_tokens=%r for model "
            "%r; treating as 0 (a reservation must be a real token count).",
            input_tokens,
            model_id,
        )
        input_tokens = 0
    elif input_tokens < 0:
        logger.warning(
            "calc_embedding_cost received negative input_tokens=%d for model %r; "
            "clamping to 0 (a reservation must never go negative).",
            input_tokens,
            model_id,
        )
        input_tokens = 0
    elif input_tokens > _MAX_EMBEDDING_INPUT_TOKENS:
        logger.warning(
            "calc_embedding_cost received implausibly large input_tokens=%d for "
            "model %r; capping at %d so float cost math cannot overflow to inf.",
            input_tokens,
            model_id,
            _MAX_EMBEDDING_INPUT_TOKENS,
        )
        input_tokens = _MAX_EMBEDDING_INPUT_TOKENS

    if model_id not in EMBEDDING_PRICING:
        estimated = True
        if model_id not in _unknown_embedding_model_warned:
            # Bound the dedup set (shared _WARNED_SET_CAP): beyond the cap of
            # distinct unknown model ids, always warn (no suppression) rather
            # than growing the set without bound. PR3 wires this into a
            # high-volume ingestion loop, so the cap matters more here than on
            # the chat path.
            if len(_unknown_embedding_model_warned) < _WARNED_SET_CAP:
                _unknown_embedding_model_warned.add(model_id)
            logger.warning(
                "unknown embedding model %r has no EMBEDDING_PRICING entry — "
                "cost estimated via fallback (max known embedding rate). "
                "Add it to EMBEDDING_PRICING to silence this warning.",
                model_id,
            )
        rate = _embedding_fallback_rate()
    else:
        estimated = False
        rate = EMBEDDING_PRICING[model_id]

    # Round UP to 6 decimals (a reservation must never under-count -- see
    # docstring). cost_usd == input_tokens * rate / 1_000_000, so
    # input_tokens * rate is exactly cost_usd in micro-dollars; ceil that to
    # whole micro-units, then divide back. Avoids importing math; -(-x // 1) is
    # float ceil.
    micro_dollars = input_tokens * rate
    cost_usd = -(-micro_dollars // 1) / 1_000_000
    return cost_usd, estimated


# Cache hit pricing — Anthropic charges 10% of input rate for cache hits
CACHE_HIT_DISCOUNT = 0.10

# Module-level set of model ids for which we've already emitted a warning,
# so operators see the message exactly once per process lifetime.
_unknown_model_warned: set[str] = set()

# Per-process dedup set for per-line corruption warnings (mirrors
# _unknown_model_warned). Key: (str(path), sub_reason) where sub_reason is one
# of the two per-line corruption kinds passed to _warn_corruption:
#   'json_decode_error', 'non_numeric_cost_usd'
# One warning per (file, reason) per process lifetime. The fail-closed paths
# (whole-file OSError, unreadable dir, >50% threshold, backend exception) call
# logger.warning directly and are NOT deduped — each is a one-time event per read.
_corruption_warned: set[tuple[str, str]] = set()


def _fallback_pricing() -> dict[str, float]:
    """Return the most expensive (conservative-pessimistic) rates from PRICING.

    Used when a model id is not in the table so that unknown models are
    over-counted rather than silently treated as free.
    """
    max_input = max(p["input"] for p in PRICING.values())
    max_output = max(p["output"] for p in PRICING.values())
    return {"input": max_input, "output": max_output}


def calc_cost(
    model: str, input_tokens: int, output_tokens: int, cache_hit_tokens: int = 0
) -> tuple[float, bool]:
    """Compute USD cost for one LLM call.

    Returns (cost_usd, cost_estimated_via_fallback).

    cost_estimated_via_fallback is True when `model` was not found in the
    PRICING table — the cost is then computed with the highest known rates
    so guardrails and dashboards remain conservative-pessimistic rather than
    zero. A one-time WARNING is logged per unseen model id.

    cache_hit_tokens is the portion of input tokens served from prompt cache;
    they cost 1/10 of the normal input rate. Remainder is normal input price.
    """
    fallback = False
    if model not in PRICING:
        fallback = True
        if model not in _unknown_model_warned:
            _unknown_model_warned.add(model)
            logger.warning(
                "unknown model %r has no pricing entry — cost estimated via "
                "fallback (highest known rates). Add it to PRICING to silence "
                "this warning and get an accurate cost.",
                model,
            )
        p = _fallback_pricing()
    else:
        p = PRICING[model]
    cache_miss_tokens = max(0, input_tokens - cache_hit_tokens)
    cost_cached = cache_hit_tokens * p["input"] * CACHE_HIT_DISCOUNT / 1_000_000
    cost_uncached = cache_miss_tokens * p["input"] / 1_000_000
    cost_output = output_tokens * p["output"] / 1_000_000
    return round(cost_cached + cost_uncached + cost_output, 6), fallback


def _warn_corruption(path: Path, sub_reason: str, msg: str) -> None:
    """Emit logger.warning for cost-read corruption, deduped by (path, sub_reason).

    Per-line corruption warnings are suppressed after the first occurrence for a
    given (file path, sub_reason) pair — prevents log flooding when an agent runs
    on a recurring schedule against the same corrupt file.

    Only the two per-line corruption kinds ('json_decode_error',
    'non_numeric_cost_usd') route through here. Fail-closed paths (whole-file
    OSError, unreadable dir, >50% threshold, backend exception) call
    logger.warning directly and are not deduped — each is a one-time event.
    """
    key = (str(path), sub_reason)
    if key in _corruption_warned:
        return
    # Bound dedup set size: beyond _WARNED_SET_CAP entries always warn (no
    # suppression).
    if len(_corruption_warned) < _WARNED_SET_CAP:
        _corruption_warned.add(key)
    logger.warning(msg)


def sum_cost_for_period(
    log_dir: Path,
    period: str,
    today: date | None = None,
    *,
    source: CostSource | None = None,
    mandate_id: str | None = None,
    backend: "LogBackend | None" = None,
    agent_name: str | None = None,
) -> CostReadResult:
    """Sum cost_usd across log records for the given period.

    Returns a CostReadResult (total_usd, degraded, dropped_records).

    period: 'today' or 'this_month'.

    Cost-read fail-closed posture (spec/09 §"Cost-read error posture"):
    - Whole-file OSError on the current-day guardrail log → degraded=True
      (gate sites treat as over-cap / fail-closed).
    - Per-line JSON or float() corruption below 50%-of-non-empty-lines →
      skip + logger.warning + degraded=True with dropped_records > 0.
    - Per-line corruption above 50%-of-non-empty-lines on the CURRENT-DAY file
      → degraded=True, total_usd=0.0 (fail-closed). A HISTORICAL file over the
      threshold → skip that file's cost + degraded=True (partial month total);
      it does not zero the whole read.
    - OSError on a prior-day (historical) file in a monthly walk → skip that
      file + degraded=True (partial month total, its real cost is silently
      dropped, so the read is flagged blind). Only the current-day file triggers
      whole-read fail-closed (total_usd=0.0) when blind; a historical blind skip
      keeps summing the rest of the month but still flips degraded so the gate
      fail-closes (fail-closed-when-blind). dropped_records stays 0 on this path
      (it's a per-line tally; a whole-file skip produces no line-level drops) —
      degraded is the load-bearing signal, not dropped_records.

    Denominator for the >50% threshold: non-empty lines only — blank lines
    are not corruption.

    source: optional filter on cost-event origin (spec/28 + spec/30):
        - None (default): sum every cost record (legacy behavior).
        - "actor": match records with cost_source == "actor" OR missing
          (legacy records pre-date the field and represent actor spend).
        - "judge" / "audit": strict match on cost_source.

    mandate_id: optional filter on mandate authorization (spec/29). When set,
        only records with cost.mandate_id == mandate_id contribute. When None,
        mandate_id is not consulted.

    backend: optional ``LogBackend`` (#61 PR 2). When set, the period sum
        is computed via ``backend.query(LogQuery(...))`` — honoring the
        operator's pinned backend (filesystem default; ``SQLiteLogBackend``
        in PR 3; future Postgres/Datadog). When ``None``, falls back to
        the legacy filesystem walk against ``log_dir`` (backward
        compatibility for any external callers + the dashboard layer
        before its readers were rewired).

    Filters AND together. Backward-compatible: omitting both kwargs preserves
    the pre-#122 behavior verbatim. When ``backend`` is provided, the
    function pushes filter predicates into ``LogQuery`` so SQL backends
    can use indexed ``WHERE`` clauses instead of materializing every
    record into the client.
    """
    today = today or date.today()

    # When the backend is the filesystem reference impl, prefer the
    # legacy file-walk semantic (file location implies date, ts content
    # ignored). This preserves the safety-load-bearing cost guardrail
    # behavior for records with malformed or missing ts — which
    # production records shouldn't have, but legacy on-disk records
    # might. SQL/Datadog backends in PR 3+ route through query() where
    # records have indexed ts and the malformed-ts case doesn't apply.
    #
    # Step 11 adversarial P0 #4 caught this: a record with ``ts="x"``
    # in today's JSONL file was counted by this function's filesystem
    # file-walk path but silently dropped by the backend.query() path —
    # a silent loosening of the cost cap. The fix preserves the file-walk
    # semantic for filesystem while still threading the backend through
    # (so the operator surface is consistent across all backend types).
    if backend is not None:
        from .logs.filesystem import FilesystemLogBackend

        if not isinstance(backend, FilesystemLogBackend):
            return _sum_via_backend(
                backend, today, period, source, mandate_id, agent_name
            )

    # Identify the current-day file path for OSError fail-closed logic.
    today_file = log_dir / today.strftime("%Y-%m") / f"{today.isoformat()}.jsonl"

    total = 0.0
    total_dropped = 0
    # had_blind_skip: set True whenever a WHOLE historical file is skipped
    # blindly (unreadable OSError, or majority-corrupt over the >50% threshold)
    # so its real cost contribution is silently dropped from the month total.
    # This is a blind spot in the monthly read distinct from the per-line
    # corruption tally (total_dropped), and it must flip degraded=True even when
    # no individual line was counted (e.g. an unreadable historical file yields
    # zero line-level drops). Fail-closed-when-blind: a dropped historical file
    # is a partial month total, so the gate must treat the read as degraded.
    had_blind_skip = False
    if period == "today":
        # Absent file = first run of the day = no cost (not blind).
        # Path.exists() PROPAGATES non-ENOENT OSErrors on Py3.12 (e.g. EACCES
        # when the parent month dir is unreadable) — guard it so an unreadable
        # current-day probe maps to degraded (blind), not a raw crash.
        try:
            today_exists = today_file.exists()
        except OSError:
            logger.warning(
                "cost-read: OSError probing current-day log %s — "
                "cost gate is BLIND, failing closed (degraded=True)",
                today_file,
            )
            return CostReadResult(total_usd=0.0, degraded=True, dropped_records=0)
        paths = [today_file] if today_exists else []
    elif period == "this_month":
        month_dir = log_dir / today.strftime("%Y-%m")
        # Path.glob() swallows the underlying os.scandir EACCES and yields an
        # EMPTY iterator when the month dir is unreadable — that would fail-OPEN
        # ($0 spent, degraded=False). Enumerate via a SINGLE os.scandir handle and
        # materialize the *.jsonl listing from it directly: this both maps an
        # unreadable month dir to BLIND (the scandir raises OSError → fail-closed)
        # AND closes the TOCTOU window that a probe-then-reglob shape would leave
        # (perms could flip between the probe and a second glob walk, and Path.glob
        # would silently swallow the late EACCES → fail-OPEN). One walk, no window.
        try:
            if month_dir.exists():
                with os.scandir(month_dir) as it:
                    paths = [
                        Path(entry.path)
                        for entry in it
                        if entry.name.endswith(".jsonl")
                    ]
            else:
                paths = []
        except OSError:
            logger.warning(
                "cost-read: OSError enumerating month dir %s — "
                "monthly cost gate is BLIND, failing closed (degraded=True)",
                month_dir,
            )
            return CostReadResult(total_usd=0.0, degraded=True, dropped_records=0)
    else:
        raise ValueError(f"unknown period: {period}")

    for path in paths:
        is_today_file = path == today_file
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            if is_today_file:
                # Current-day guardrail log is unreadable → gate is blind.
                logger.warning(
                    "cost-read: OSError reading current-day log %s — "
                    "cost gate is BLIND, failing closed (degraded=True)",
                    path,
                )
                return CostReadResult(total_usd=0.0, degraded=True, dropped_records=0)
            else:
                # Historical file — skip the whole file, but don't zero the
                # whole read. The skip drops this file's real cost contribution
                # silently, so mark the month read degraded (blind spot) — the
                # gate fail-closes, but the rest of the month still sums. This
                # is symmetric with the historical >50%-corrupt path below: both
                # are whole-file blind skips that set had_blind_skip.
                logger.warning(
                    "cost-read: OSError reading historical log %s — "
                    "skipping file (degraded=True, partial month total)",
                    path,
                )
                had_blind_skip = True
                continue

        # Per-file corruption tally. A historic file that is unreadable or
        # majority-corrupt is treated symmetrically: skip its contribution +
        # set degraded (partial month total), but do NOT zero the whole read.
        # Only the CURRENT-DAY guardrail file fails the whole read closed when
        # blind/majority-corrupt — that is the file the gate must trust.
        file_total_lines = 0
        file_corrupt_lines = 0
        file_cost = 0.0
        file_dropped = 0

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            file_total_lines += 1

            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                file_corrupt_lines += 1
                file_dropped += 1
                _warn_corruption(
                    path,
                    "json_decode_error",
                    f"cost-read: unparseable JSON line in {path} — "
                    "skipping line (counts toward corruption tally)",
                )
                continue

            if source is not None:
                rec_source = rec.get("cost_source", "actor")
                if source == "actor":
                    # legacy records (no cost_source) count as actor
                    if rec_source != "actor":
                        continue
                else:
                    if rec_source != source:
                        continue
            if mandate_id is not None:
                if rec.get("mandate_id") != mandate_id:
                    continue

            cost_val = rec.get("cost_usd", 0.0)
            # Reject bools BEFORE float(): float(True) == 1.0 would silently count
            # a JSON `true` cost as a $1.00 charge (and `false` as $0), inflating
            # or hiding spend rather than surfacing the malformed value. A boolean
            # cost is corruption, not a number — it must flow through the same
            # dropped/degraded path as any other non-numeric cost_usd.
            if isinstance(cost_val, bool):
                file_corrupt_lines += 1
                file_dropped += 1
                _warn_corruption(
                    path,
                    "non_numeric_cost_usd",
                    f"cost-read: boolean cost_usd in {path} — "
                    "skipping line (counts toward corruption tally)",
                )
                continue
            try:
                file_cost += float(cost_val)
            except (TypeError, ValueError):
                file_corrupt_lines += 1
                file_dropped += 1
                _warn_corruption(
                    path,
                    "non_numeric_cost_usd",
                    f"cost-read: non-numeric cost_usd in {path} — "
                    "skipping line (counts toward corruption tally)",
                )
                continue

        # An empty (0-byte / all-blank) file is READABLE but carries no logged
        # cost yet — semantically identical to an ABSENT file (no cost), NOT to a
        # corrupt or unreadable one. Genuine blindness is the OSError path above;
        # "no content" is not blind. Critically, the log writer's open("a")
        # (atomic_append_jsonl) CREATES the file before the first write+fsync, so
        # a concurrent reader hits a legitimate 0-byte window on every first
        # append of the day — failing closed there would spuriously block a
        # legitimate call on a normal append race. Treat empty the same for the
        # current-day and historical files: skip, no cost, no degradation.
        if file_total_lines == 0:
            continue

        # >50%-of-non-empty-lines threshold, applied per-file.
        if file_corrupt_lines > 0.5 * file_total_lines:
            if is_today_file:
                # Current-day guardrail file is majority-corrupt → the gate
                # cannot trust today's spend → fail the whole read closed.
                logger.warning(
                    "cost-read: %d/%d lines corrupt in current-day log %s "
                    "(>50%% threshold) — failing closed (degraded=True)",
                    file_corrupt_lines,
                    file_total_lines,
                    path,
                )
                return CostReadResult(
                    total_usd=0.0, degraded=True, dropped_records=file_corrupt_lines
                )
            # Historical file over threshold: skip its (untrustworthy) cost
            # contribution and mark degraded, but keep summing the rest of the
            # month — symmetric with the historical-OSError skip above. A single
            # garbage old daily log must NOT brick the gate for the whole month.
            logger.warning(
                "cost-read: %d/%d lines corrupt in historical log %s "
                "(>50%% threshold) — skipping file's cost (degraded=True, "
                "partial month total)",
                file_corrupt_lines,
                file_total_lines,
                path,
            )
            total_dropped += file_dropped
            had_blind_skip = True
            continue

        total += file_cost
        total_dropped += file_dropped

    # degraded=True when EITHER per-line corruption was skipped (total_dropped>0,
    # below-threshold skips on any file) OR a whole historical file was skipped
    # blindly (had_blind_skip: unreadable OSError or >50%-corrupt historical
    # file, whose real cost is silently dropped from the month total). Clean
    # reads return degraded=False. The whole-file blind skip is the audit signal
    # the gate consumes (fail-closed-when-blind, Principle #4/#5); total_dropped
    # alone would miss an unreadable historical file that produced zero line-level
    # drops, leaving the monthly cap silently under-counting.
    degraded = total_dropped > 0 or had_blind_skip
    return CostReadResult(
        total_usd=total, degraded=degraded, dropped_records=total_dropped
    )


def _sum_via_backend(
    backend: "LogBackend",
    today: date,
    period: str,
    source: CostSource | None,
    mandate_id: str | None,
    agent_name: str | None = None,
) -> CostReadResult:
    """Sum cost_usd via LogBackend.query (PR 2 backend-routed path).

    Returns CostReadResult. Any backend exception → degraded=True / fail-closed.

    Uses ISO-8601 lexicographic comparison via LogQuery.since/until —
    backends with index pushdown (SQLite PR 3 forward) translate this
    to ``WHERE ts >= :since AND ts < :until`` natively. Filesystem
    backend walks month dirs as before.

    ``agent_name`` filter is critical for shared-backend deployments
    (#61 PR 3 review-pass Step 11 P0 #1) — without it, alice's cost
    guardrails sum bob's records too. The filesystem path's
    one-dir-per-agent shape provides this naturally; shared backends
    require the explicit filter.
    """
    from .logs import LogQuery

    if period == "today":
        # Local-tz day boundaries — matches the legacy idiom where
        # ``log_path = log_dir / today.strftime("%Y-%m") / today.isoformat() ``
        # selects records whose filename matches the local date.
        since_dt = datetime.combine(today, time.min).astimezone()
        until_dt = datetime.combine(today, time.max).astimezone()
    elif period == "this_month":
        first_of_month = today.replace(day=1)
        # Next month's first day, then back off to last microsecond.
        if today.month == 12:
            next_month = date(today.year + 1, 1, 1)
        else:
            next_month = date(today.year, today.month + 1, 1)
        since_dt = datetime.combine(first_of_month, time.min).astimezone()
        until_dt = datetime.combine(next_month, time.min).astimezone()
    else:
        raise ValueError(f"unknown period: {period}")

    try:
        records = list(
            backend.query(
                LogQuery(
                    since=since_dt,
                    until=until_dt,
                    cost_source=source,
                    mandate_id=mandate_id,
                    agent_name=agent_name,
                )
            )
        )
    except LogBackendReadError as exc:
        # Typed catch — conforming backend raised LogBackendReadError,
        # signalling a genuine unrecoverable read failure (corruption /
        # I/O error / lost connection after all retries). Clean log.
        logger.warning(
            "cost-read: backend raised LogBackendReadError (genuine "
            "unrecoverable read failure): %s — gate is BLIND, "
            "failing closed (degraded=True)",
            exc,
        )
        return CostReadResult(total_usd=0.0, degraded=True, dropped_records=0)
    except Exception as exc:  # noqa: BLE001  # backend contract does not constrain exc type
        # Broad backstop — non-conforming backend raised an unexpected
        # exception type. Fail closed, same shape as the typed path.
        logger.warning(
            "cost-read: backend.query() raised unexpected %s: %s — "
            "gate is BLIND, failing closed (degraded=True)",
            type(exc).__name__,
            exc,
        )
        return CostReadResult(total_usd=0.0, degraded=True, dropped_records=0)

    total = 0.0
    dropped = 0
    # The backend denominator counts only cost-BEARING records (cost_usd not
    # None); None-cost audit records must not dilute the threshold. This is a
    # DELIBERATELY different population from the filesystem path's denominator:
    # the fs reader counts ALL non-empty lines (including audit/non-cost records
    # and records later filtered out by source/mandate), because on disk it
    # cannot cheaply distinguish them before parsing, whereas the backend has
    # already applied the since/until/source/mandate filters server-side. The
    # backend path is therefore the stricter of the two (a smaller denominator
    # trips the >50% threshold sooner) — that asymmetry is safe (stricter never
    # fails OPEN); it is not a parity guarantee. spec/09 only normatively
    # specifies the fs denominator ("50% of non-empty lines"); the backend
    # denominator is an implementation choice consistent with fail-closed-when-blind.
    cost_bearing = 0

    for r in records:
        # DEFENSIVE BELT, normally dead path. Every shipped backend yields a
        # cost_usd already coerced to float | None — either via a typed DB column
        # (SQLite REAL, Postgres DOUBLE PRECISION) or via RunRecord.from_dict's
        # _coerce_optional_float (logs/types.py), which turns a non-numeric string
        # into None upstream. So a non-numeric cost_usd never reaches this loop
        # from a real backend: it is coerced to None and skipped by the guard
        # below. The try/except + bad-record accounting only fires for a
        # misbehaving custom backend that hands back un-coerced raw objects
        # (e.g. the SimpleNamespace records in tests/test_costs.py). It is kept so
        # ANY backend misbehavior fail-closes via the >50% threshold rather than
        # crashing the gate, matching the broad except around backend.query().
        try:
            # Read the value first. An AttributeError here (duck-typed record with
            # no cost_usd) is a malformed record → drop it (still cost-bearing for
            # threshold purposes — a record we cannot read a cost from is exactly
            # what the >50% blind-read threshold exists to catch).
            cost = r.cost_usd
        except AttributeError:
            cost_bearing += 1
            dropped += 1
            logger.warning(
                "cost-read: un-coerced/malformed backend record (run_id=%s) — "
                "skipping (counts toward corruption tally)",
                getattr(r, "run_id", "?"),
            )
            continue
        if cost is None:
            # None-cost (audit) records are not cost-bearing and never dilute the
            # threshold denominator — skipped without counting.
            continue
        cost_bearing += 1
        # Reject bools BEFORE the add (parity with the fs path's bool reject):
        # bool is a subclass of int, so `total += True` succeeds as +1.0 and
        # `+= False` as +0.0 — a boolean cost_usd would silently mis-count spend
        # rather than being surfaced. Treat it as corruption so it flows through
        # the same >50% fail-closed / degraded logic. (Unreachable for shipped
        # backends — _coerce_optional_float never yields a bool — but keeps the
        # two cost-read paths symmetric so the spec/09 "same net result" claim
        # holds for every malformed value, not just strings.)
        if isinstance(cost, bool):
            dropped += 1
            logger.warning(
                "cost-read: boolean cost_usd in backend record (run_id=%s) — "
                "skipping (counts toward corruption tally)",
                getattr(r, "run_id", "?"),
            )
            continue
        try:
            total += cost
        except (TypeError, ValueError):
            # A value that will not add as a float (un-coerced str/Decimal from a
            # misbehaving backend) counts toward the dropped tally — already
            # counted toward cost_bearing above — so it flows through the same
            # >50% fail-closed / degraded logic instead of escaping uncaught.
            dropped += 1
            logger.warning(
                "cost-read: un-coerced/non-numeric cost_usd in backend record "
                "(run_id=%s) — skipping (counts toward corruption tally)",
                getattr(r, "run_id", "?"),
            )

    # Apply >50% threshold to backend records (denominator is the cost-bearing
    # population — see the note above; stricter than, not identical to, the fs path).
    if cost_bearing > 0 and dropped > 0.5 * cost_bearing:
        logger.warning(
            "cost-read: %d/%d cost-bearing backend records non-numeric "
            "(>50%% threshold) — failing closed (degraded=True)",
            dropped,
            cost_bearing,
        )
        return CostReadResult(total_usd=0.0, degraded=True, dropped_records=dropped)

    degraded = dropped > 0
    return CostReadResult(total_usd=total, degraded=degraded, dropped_records=dropped)


def load_warning_state(state_path: Path) -> dict:
    """Load the per-agent warning fired-state. Used to make warnings idempotent."""
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_warning_state(state_path: Path, state: dict) -> None:
    """Save the warning fired-state. Atomic write via temp+rename."""
    from ._io import atomic_write

    atomic_write(state_path, json.dumps(state, indent=2))

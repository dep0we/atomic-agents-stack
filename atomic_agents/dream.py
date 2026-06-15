"""Memory consolidation pipeline — atomic_agents.dream.

Inspired by Anthropic's Dreams API (managed-agents/dreams). An agent "dreams"
between sessions: reads memory/ + journal/ + log/, detects duplicates,
contradictions, stale notes, and promotable journal observations, then produces
a NEW parallel <agent>/dreams/<id>/memory/ directory the operator can review and
either apply (atomic swap) or discard.

Public API:
    DreamRunner(agents_root, agent_name, model=None)
        .start(journal_lookback_days, log_lookback_days, instructions, critical)
        .status(dream_id=None)
        .review(dream_id)
        .apply(dream_id)
        .discard(dream_id)
        .list_dreams()

Storage layout:
    <agent>/dreams/
    └── drm_<YYYY-MM-DDTHHMMSS>_<6hex>/
        ├── memory/
        │   ├── INDEX.md
        │   └── <notes>.md
        ├── report.md
        └── manifest.json

CLI (python -m atomic_agents.dream):
    <agent>                          start
    <agent> --status [<id>]         status of id or most recent
    <agent> --review <id>           print report.md
    <agent> --apply <id>            swap memory/ ↔ dreamed
    <agent> --discard <id>          rm dream dir
    <agent> --list                  list all dreams
    <agent> --instructions "..."    focus hint for synthesis
    <agent> --journal-lookback 60   days to include from journal
    <agent> --log-lookback 60       days to include from log
    <agent> --critical              bypass cost cap
    <agent> --model <id>            override model

Exit codes: 0 success, 1 failed/canceled, 2 cost-guardrail-blocked.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
from dataclasses import dataclass, asdict
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .logs import LogBackend

    # NOTE: MemoryBackend is imported unconditionally at runtime below (used by
    # the isinstance guard in DreamRunner.__init__), so it is intentionally NOT
    # re-imported here — a TYPE_CHECKING-only re-import would be dead.
    from .profile import AgentProfileBackend
    from .registry import ToolRegistryBackend
    from .mandate import MandateBackend
    from .policy import PolicyBackend
    from .persona import PersonaBackend
    from .corpus import CorpusBackend
    from .mcp_registry import MCPServerRegistryBackend
    from .journal.backend import JournalBackend

import frontmatter

from . import _costs, _llm, _model
from ._capture import _render_note
from ._io import atomic_write
from .locks import (
    LockBackend,
    LockBusy,
    check_lock_lost,
    get_default_lock_backend,
)
from ._platform import get_agents_root
from .exceptions import AtomicAgentsError, DreamInProgress, DreamNotFound
from .memory.backend import MemoryBackend, WritePolicy
from .memory.filesystem import FilesystemBackend, FilesystemStagedMemory
from .memory import get_default_memory_backend
from .types import Capture

# Regex for valid dream_id: only the drm_<YYYY-MM-DDTHHMMSS>_<6hex> shape or
# any sequence of alphanumeric + underscore + hyphen with no path separators.
# This prevents path traversal via dream_id such as "../../persona".
import re as _re

# Module logger. Declared after the deferred ``import re as _re`` above so that
# inserting it does not push the (pre-existing) deferred import below a non-import
# statement and trip ruff E402.
_logger = logging.getLogger(__name__)

_VALID_DREAM_ID_RE = _re.compile(r"^[a-zA-Z0-9_-]+$")


# ──────────────────────────────────────────────────────────────────
# Public data classes


@dataclass
class DreamInputs:
    memory_count: int
    journal_lookback_days: int
    journal_count: int
    log_lookback_days: int
    log_line_count: int


@dataclass
class ConsolidatedNote:
    new: str  # filename of the consolidated note
    supersedes: list[str]  # filenames of notes it replaces
    reason: str  # one-sentence explanation


@dataclass
class PromotedNote:
    new: str
    from_journal_entries: list[str]  # journal file names
    reason: str


@dataclass
class StaleMarking:
    note: str
    new_expires_at: str  # ISO date


@dataclass
class DreamResult:
    dream_id: str
    agent_name: str
    status: str  # pending | running | completed | failed | canceled
    model: str
    instructions: str
    inputs: DreamInputs
    output_memory_count: int  # 0 until completed
    consolidated: list[ConsolidatedNote]
    promoted: list[PromotedNote]
    marked_stale: list[StaleMarking]
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: float
    started_at: str
    ended_at: str | None
    error: str | None = None
    applied_at: str | None = None
    archived_path: str | None = None


# ──────────────────────────────────────────────────────────────────
# Dream lock — replaced in #60 PR 2.
#
# The internal ``_DreamLock`` class used raw ``fcntl.flock`` against
# ``<dreams_dir>/.lock``. It is now a ``FilesystemLockBackend(dreams_dir)
# .acquire("")`` call wired in ``DreamRunner.__init__`` below. The
# on-disk artifact is unchanged (still ``<dreams_dir>/.lock`` with
# the legacy ``pid=<pid> acquired=<ts>`` payload). Operators / external
# diagnostic scripts that probed that path keep working.
#
# Domain exception: ``LockBusy`` from the backend is wrapped in
# ``DreamInProgress`` at the call site (via ``raise ... from exc``) so
# the existing semantic — "another dream pipeline is in progress" —
# stays distinct from the agent's main-lock exception. See
# ``docs/spec/21-lock-backend.md`` §"What this PR does NOT do".


# ──────────────────────────────────────────────────────────────────
# ID generation


def _new_dream_id() -> str:
    ts = datetime.now().strftime("%Y-%m-%dT%H%M%S")
    hex6 = secrets.token_hex(3)
    return f"drm_{ts}_{hex6}"


# ──────────────────────────────────────────────────────────────────
# Manifest I/O


def _manifest_to_dict(result: DreamResult) -> dict:
    d = asdict(result)
    # Convert nested DreamInputs dataclass
    return d


def _dict_to_dream_result(d: dict) -> DreamResult:
    inputs = DreamInputs(**d.pop("inputs"))
    consolidated = [ConsolidatedNote(**c) for c in d.pop("consolidated")]
    promoted = [PromotedNote(**p) for p in d.pop("promoted")]
    marked_stale = [StaleMarking(**s) for s in d.pop("marked_stale")]
    return DreamResult(
        inputs=inputs,
        consolidated=consolidated,
        promoted=promoted,
        marked_stale=marked_stale,
        **d,
    )


def _write_manifest(dream_dir: Path, result: DreamResult) -> None:
    manifest_path = dream_dir / "manifest.json"
    content = json.dumps(_manifest_to_dict(result), indent=2, default=str)
    atomic_write(manifest_path, content)


def _read_manifest(dream_dir: Path) -> DreamResult:
    manifest_path = dream_dir / "manifest.json"
    if not manifest_path.exists():
        raise DreamNotFound(f"No manifest.json in {dream_dir}")
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    return _dict_to_dream_result(data)


# ──────────────────────────────────────────────────────────────────
# Input reading helpers


def _read_memory_notes(agent_root: Path) -> list[dict]:
    """Return list of parsed notes from memory/. Each dict has filename, meta, body."""
    memory_dir = agent_root / "memory"
    notes = []
    if not memory_dir.exists():
        return notes
    for path in sorted(memory_dir.glob("*.md")):
        if path.name == "INDEX.md":
            continue
        try:
            parsed = frontmatter.load(path)
            notes.append(
                {
                    "filename": path.name,
                    "meta": dict(parsed.metadata),
                    "body": parsed.content,
                }
            )
        except Exception:
            continue
    return notes


def _read_memory_notes_via_backend(backend: "MemoryBackend") -> list[dict]:
    """Return list of parsed notes from memory/ via MemoryBackend protocol.

    Replaces the direct filesystem glob in _read_memory_notes() when a backend
    is available. Returns the same dict format ({filename, meta, body}).
    """
    notes = []
    for ref in backend.list_notes(include_archived=True, include_superseded=True):
        note = backend.read_note(ref.name)
        if note is None:
            continue
        notes.append(
            {
                "filename": ref.name,
                "meta": {
                    "type": note.type,
                    "name": note.name,
                    "description": note.description,
                    "confidence": note.confidence,
                    "sources": note.sources,
                    "captured": note.captured.isoformat() if note.captured else None,
                    "last_seen": note.last_seen.isoformat() if note.last_seen else None,
                    "pinned": note.pinned,
                    "archived": note.archived,
                    "superseded_by": note.superseded_by,
                    "expires_at": note.expires_at,
                    "tags": note.tags,
                    **note.extra_frontmatter,
                },
                "body": note.body,
            }
        )
    return notes


# NOTE: the former _read_journal_entries helper was removed in #427 PR1. Its
# date-window read now lives in JournalBackend.query_by_date(); the two dream
# call sites adapt JournalEntry → {"filename", "text"} dicts and pass
# end=date.max to preserve the legacy lower-bound-only window (future-dated
# entries included).


def _read_log_lines(
    agent_root: Path,
    lookback_days: int,
    *,
    log_backend: "LogBackend | None" = None,
    agent_name: str | None = None,
) -> list[dict]:
    """Return non-helper log records within lookback window.

    When ``log_backend`` is provided (#61 PR 2), reads via
    ``backend.query(LogQuery(since=cutoff))`` and filters helpers
    in-memory — same record shape as the legacy walk via
    ``RunRecord.to_dict()``. Falls back to the legacy filesystem walk
    when ``log_backend`` is None for backward compatibility (any
    callers reading ``<agent>/log/`` directly).
    """
    cutoff = date.today() - timedelta(days=lookback_days)

    if log_backend is not None:
        from .logs import LogQuery, LogBackendReadError

        since_dt = datetime.combine(cutoff, dt_time.min).astimezone()
        records = []
        # spec/22 read-failure addendum (issue #497): query() now raises
        # LogBackendReadError on an unrecoverable read failure. This read runs
        # BEFORE the dream cost gate (_check_cap) and any LLM batch, so a raise
        # here cannot leak uncosted spend — but it WOULD hard-crash a dream run.
        # Dream consolidation is analysis, not a control gate: degrade gracefully
        # (lose the log signal, complete the run) rather than crash, matching how
        # the cost reader and dashboard treat the same blind read.
        try:
            query_iter = log_backend.query(
                LogQuery(since=since_dt, agent_name=agent_name)
            )
        except LogBackendReadError as exc:
            _logger.warning(
                "dream: log read for agent %r raised LogBackendReadError "
                "(blind read): %s — proceeding with empty log signal",
                agent_name,
                exc,
            )
            return records
        for rec in query_iter:
            # filter out helper runs — too noisy. Belt-and-suspenders:
            # check BOTH primitive (post-PR-2 records) AND trigger
            # (legacy pre-PR-2 records that don't have primitive set).
            # Matches dashboard/quality._count_provenance pattern —
            # Step 9.1 maintainability + Step 11 P0 #2 mitigation.
            if rec.primitive == "helper" or rec.trigger == "helper":
                continue
            records.append(rec.to_dict())
        return records

    log_dir = agent_root / "log"
    records = []
    if not log_dir.exists():
        return records
    for month_dir in sorted(log_dir.iterdir()):
        if not month_dir.is_dir():
            continue
        for log_file in sorted(month_dir.glob("*.jsonl")):
            try:
                file_date = date.fromisoformat(log_file.stem)
                if file_date < cutoff:
                    continue
            except (ValueError, TypeError):
                pass
            try:
                text = log_file.read_text(encoding="utf-8")
            except OSError:
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    # filter out helper runs — too noisy
                    if rec.get("trigger") == "helper":
                        continue
                    records.append(rec)
                except json.JSONDecodeError:
                    continue
    return records


# ──────────────────────────────────────────────────────────────────
# Detection helpers (pure logic — no LLM)

STALE_THRESHOLD_DAYS = 90
STALE_EXPIRES_EXTEND_DAYS = 30


def _detect_stale_notes(
    notes: list[dict], today: date | None = None
) -> list[StaleMarking]:
    """Mechanical stale detection: notes older than threshold, not pinned."""
    today = today or date.today()
    cutoff = today - timedelta(days=STALE_THRESHOLD_DAYS)
    markings: list[StaleMarking] = []
    for note in notes:
        meta = note["meta"]
        if meta.get("pinned"):
            continue
        if meta.get("archived"):
            continue
        if meta.get("superseded_by"):
            continue
        last_seen = meta.get("last_seen")
        if not last_seen:
            continue
        try:
            ls_date = (
                datetime.fromisoformat(str(last_seen)).date()
                if "T" in str(last_seen)
                else date.fromisoformat(str(last_seen))
            )
        except (ValueError, TypeError):
            continue
        if ls_date < cutoff:
            new_expires = (
                today + timedelta(days=STALE_EXPIRES_EXTEND_DAYS)
            ).isoformat()
            markings.append(
                StaleMarking(note=note["filename"], new_expires_at=new_expires)
            )
    return markings


def _cluster_by_type_and_name(notes: list[dict]) -> list[list[dict]]:
    """Group notes by type, then cluster ones with similar names.

    Two notes are in the same cluster if they share the same type AND their
    normalised name tokens overlap ≥ 50%.
    """
    from collections import defaultdict

    by_type: dict[str, list[dict]] = defaultdict(list)
    for note in notes:
        note_type = note["meta"].get("type", "unknown")
        by_type[note_type].append(note)

    clusters: list[list[dict]] = []
    for type_notes in by_type.values():
        ungrouped = list(type_notes)
        while ungrouped:
            seed = ungrouped.pop(0)
            cluster = [seed]
            seed_tokens = _name_tokens(seed["meta"].get("name", seed["filename"]))
            remaining = []
            for candidate in ungrouped:
                cand_tokens = _name_tokens(
                    candidate["meta"].get("name", candidate["filename"])
                )
                if seed_tokens and cand_tokens:
                    overlap = len(seed_tokens & cand_tokens) / min(
                        len(seed_tokens), len(cand_tokens)
                    )
                    if overlap >= 0.5:
                        cluster.append(candidate)
                        continue
                remaining.append(candidate)
            ungrouped = remaining
            clusters.append(cluster)
    return clusters


def _name_tokens(name: str) -> set[str]:
    """Lower-case word-tokens from a name string."""
    import re

    tokens = re.findall(r"[a-z0-9]+", name.lower())
    # Remove very short/common tokens
    stopwords = {"a", "an", "the", "is", "in", "at", "of", "to", "and", "or"}
    return {t for t in tokens if len(t) > 2 and t not in stopwords}


# ──────────────────────────────────────────────────────────────────
# LLM-based detection helpers

_HAIKU = "claude-haiku-4-5-20251001"


def _build_duplicate_prompts(
    clusters: list[list[dict]],
) -> tuple[list[str], list[list[dict]]]:
    """Build one prompt per multi-note cluster. Return (prompts, matching_clusters)."""
    prompts = []
    active_clusters = []
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        notes_text = "\n\n".join(
            f"### {n['filename']}\nname: {n['meta'].get('name', '')}\n\n{n['body'][:600]}"
            for n in cluster
        )
        prompts.append(
            f"You are reviewing atomic memory notes for an AI agent.\n\n"
            f"These notes have the same type and similar names. "
            f"Determine if they describe the same observation.\n\n"
            f"{notes_text}\n\n"
            f"Respond with JSON only:\n"
            f'{{"is_duplicate": true/false, "merged_body": "...(if duplicate) or null", '
            f'"merged_name": "...(if duplicate) or null"}}'
        )
        active_clusters.append(cluster)
    return prompts, active_clusters


def _build_contradiction_prompts(
    notes: list[dict], journal_entries: list[dict]
) -> list[str]:
    """Build prompts to check recent journal entries against memory notes for contradictions."""
    prompts = []
    # Only consider a sample to keep cost manageable
    sample_journals = journal_entries[:5]
    sample_notes = notes[:20]
    for journal in sample_journals:
        notes_summary = "\n".join(
            f"- [{n['filename']}] {n['meta'].get('name', '')}: {n['body'][:200]}"
            for n in sample_notes
        )
        prompts.append(
            f"You are checking an AI agent's journal entry against its memory notes.\n\n"
            f"Journal entry ({journal['filename']}):\n{journal['text'][:1000]}\n\n"
            f"Memory notes:\n{notes_summary}\n\n"
            f"Identify any memory notes that are contradicted or significantly outdated "
            f"by the journal entry. Respond with JSON only:\n"
            f'{{"contradictions": ['
            f'{{"note": "filename.md", "resolved_value": "what should replace it"}}]}}'
        )
    return prompts


def _build_promotion_prompts(
    journal_entries: list[dict], notes: list[dict]
) -> list[str]:
    """Build prompts to cluster journal entries and find promotion candidates."""
    if not journal_entries:
        return []
    # Build a set of existing note names for reference
    existing_names = {n["meta"].get("name", "") for n in notes}
    existing_block = ", ".join(sorted(existing_names)[:30]) or "(none)"
    journals_text = "\n\n---\n\n".join(
        f"{j['filename']}:\n{j['text'][:600]}" for j in journal_entries[:10]
    )
    return [
        f"You are reviewing journal entries for an AI agent.\n\n"
        f"Journal entries:\n{journals_text}\n\n"
        f"Existing memory note names: {existing_block}\n\n"
        f"Identify recurring observations across multiple journal entries that are "
        f"NOT already in memory. These are candidates for promotion to atomic memory notes.\n\n"
        f"Respond with JSON only:\n"
        f'{{"promotions": ['
        f'{{"from_entries": ["filename.md", ...], "name": "...", "type": "feedback|project|decision|reference|user", '
        f'"body": "markdown body for the new note"}}'
        f"]}}"
    ]


def _parse_helper_text(text: str) -> dict:
    """Parse JSON from a helper response. Returns empty dict on failure."""
    text = text.strip()
    # Handle ```json ... ``` fences
    import re

    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


# ──────────────────────────────────────────────────────────────────
# Output writing helpers


def _write_note_to_dir(
    target_dir: Path,
    capture: Capture,
    today: date,
    supersedes: list[str] | None = None,
) -> str:
    """Write one memory note to target_dir. Returns the filename written."""
    from ._schema import derive_filename

    filename = derive_filename(capture.type, capture.name)
    target = target_dir / filename
    content = _render_note(capture, today)
    # If we need to add supersedes list (multiple), patch frontmatter post-render
    if supersedes and len(supersedes) > 1:
        # Re-parse and add supersedes_list field
        post = frontmatter.loads(content)
        post.metadata["supersedes"] = supersedes[0]
        post.metadata["supersedes_list"] = supersedes
        content = frontmatter.dumps(post) + "\n"
    atomic_write(target, content)
    return filename


def _copy_note_to_dir(
    target_dir: Path,
    filename: str,
    meta: dict,
    body: str,
    today: date,
    new_expires_at: str | None = None,
) -> str:
    """Copy an existing note (possibly updating expires_at) to target_dir."""
    target = target_dir / filename
    if new_expires_at:
        meta = dict(meta)
        meta["expires_at"] = new_expires_at
    post = frontmatter.Post(body, **meta)
    content = frontmatter.dumps(post) + "\n"
    atomic_write(target, content)
    return filename


def _build_fresh_index(memory_dir: Path, notes: list[str]) -> str:
    """Build a fresh INDEX.md content from the notes in memory_dir."""
    from ._capture import _section_for_type
    from collections import defaultdict

    sections: dict[str, list[str]] = defaultdict(list)
    for filename in notes:
        path = memory_dir / filename
        if not path.exists():
            continue
        try:
            parsed = frontmatter.load(path)
            note_type = parsed.metadata.get("type", "reference")
            name = parsed.metadata.get("name", filename)
            desc = parsed.metadata.get("description", "")
            sections[note_type].append(f"- [{name}]({filename}) — {desc}")
        except Exception:
            continue

    lines = ["# Memory Index\n"]
    for type_key in ("user", "feedback", "project", "decision", "reference"):
        if type_key in sections:
            section_name = _section_for_type(type_key)
            lines.append(f"\n## {section_name}\n")
            lines.extend(sections[type_key])
    return "\n".join(lines) + "\n"


def _build_report(
    consolidated: list[ConsolidatedNote],
    promoted: list[PromotedNote],
    stale: list[StaleMarking],
    unchanged_count: int,
) -> str:
    """Build the human-readable report.md content."""
    lines = ["# Dream Report\n"]
    lines.append(f"Generated: {datetime.now().isoformat()}\n")
    lines.append("\n## Summary\n")
    lines.append(f"- Consolidated: {len(consolidated)}")
    lines.append(f"- Promoted from journal: {len(promoted)}")
    lines.append(f"- Marked stale: {len(stale)}")
    lines.append(f"- Unchanged: {unchanged_count}\n")

    if consolidated:
        lines.append("\n## Consolidated Notes\n")
        for c in consolidated:
            lines.append(f"### {c.new}")
            lines.append(f"**Supersedes:** {', '.join(c.supersedes)}")
            lines.append(f"**Reason:** {c.reason}\n")

    if promoted:
        lines.append("\n## Promoted from Journal\n")
        for p in promoted:
            lines.append(f"### {p.new}")
            lines.append(
                f"**From journal entries:** {', '.join(p.from_journal_entries)}"
            )
            lines.append(f"**Reason:** {p.reason}\n")

    if stale:
        lines.append("\n## Marked Stale\n")
        for s in stale:
            lines.append(f"- `{s.note}` → expires_at set to `{s.new_expires_at}`")

    return "\n".join(lines) + "\n"


# ──────────────────────────────────────────────────────────────────
# Cost estimation


def _estimate_dream_cost(
    model: str,
    notes: list[dict],
    journal_entries: list[dict],
    log_lines: list[dict],
) -> float:
    """Rough upfront cost estimate for a dream run."""
    # Token estimation: ~4 chars per token
    mem_chars = sum(len(n["body"]) + 200 for n in notes)
    journal_chars = sum(len(j["text"]) for j in journal_entries)
    log_chars = sum(len(json.dumps(r)) for r in log_lines[:100])
    total_input_chars = mem_chars + journal_chars + log_chars
    est_input_tokens = total_input_chars // 4

    # Estimate 3 passes (detection + synthesis) with ~1024 output tokens each
    est_output_tokens = 1024 * 3

    pricing = _costs.PRICING.get(model, _costs._fallback_pricing())
    input_cost = est_input_tokens * pricing["input"] / 1_000_000
    output_cost = est_output_tokens * pricing["output"] / 1_000_000
    return round(input_cost + output_cost, 6)


def _check_cap(
    agent_root: Path,
    model: str,
    reserved: float,
    critical: bool,
    *,
    log_backend: "LogBackend | None" = None,
    agent_name: str | None = None,
    model_config: dict | None = None,
) -> None:
    """Raise ValueError if reserved cost exceeds remaining headroom (unless critical).

    Per #61 PR 2: when ``log_backend`` is provided, the cost sums are
    computed via the backend's ``query()`` rather than walking the
    filesystem directly. Honors the operator's pinned LogBackend
    (filesystem default; SQLiteLogBackend in PR 3 forward).

    Per #63 PR 2 Decision 2: when ``model_config`` is provided, the
    caps are read from the already-resolved dict (the same one
    DreamRunner pulled from ``profile_backend.load_profile().model_config``)
    instead of re-reading model.md from disk. This is the load-bearing
    fix for Step 11 P1#3 — the cost-guardrail call site was the OTHER
    model.md read in dream.py (alongside DreamRunner.__init__:1128) and
    BOTH must route through the profile backend for operator-pinned
    backends to apply consistently. Falls back to the legacy
    ``_model.parse_model_md`` read when ``model_config`` is None, so
    pre-PR-2 callers (none in core; safety belt) continue to work.
    """
    if critical or reserved <= 0:
        return
    # Resolve the model config and gate on cost_guardrails_enabled BEFORE
    # reading cost, mirroring _check_cost_guardrails' ordering (agent.py).
    # A guardrails-DISABLED agent must never be fail-closed by a degraded
    # read — spec/09 scopes the degraded→fail-closed mapping to
    # guardrails-ENABLED agents only, and the sibling gate returns allow=True
    # before any cost read. Reading first would brick a default (guardrails-off)
    # dream on a single corrupt current-day log.
    if model_config is not None:
        model_data = model_config
    else:
        model_data = _model.parse_model_md(agent_root / "model.md")
    if not model_data.get("cost_guardrails_enabled"):
        return
    log_dir = agent_root / "log"
    today_result = _costs.sum_cost_for_period(
        log_dir, "today", source="actor", backend=log_backend, agent_name=agent_name
    )
    month_result = _costs.sum_cost_for_period(
        log_dir,
        "this_month",
        source="actor",
        backend=log_backend,
        agent_name=agent_name,
    )
    today_cost = today_result.total_usd
    month_cost = month_result.total_usd
    daily_cap = model_data.get("daily_cap_usd", 0.0)
    monthly_cap = model_data.get("monthly_cap_usd", 0.0)
    # Gate site: fail-closed on a degraded read ONLY when there is a cap to
    # enforce (same uncapped-skip POSTURE as _check_cost_guardrails — #495 P2,
    # but a model.md-only predicate: dream reads caps from model_data and does
    # not enforce Policy caps, so the predicate matches that surface). An
    # uncapped dream agent's headroom is inf (the reservation can never exceed
    # it), so a blind read changes nothing; blocking it would be a spurious
    # refusal. This is a gate, not a reporting path: when a cap IS set and the
    # data is blind, refuse rather than render a partial number.
    if (daily_cap > 0 or monthly_cap > 0) and (
        today_result.degraded or month_result.degraded
    ):
        raise ValueError(
            "cost data unreadable — dream cost gate fail-closed. "
            "Use critical=True to bypass."
        )
    daily_remaining = (daily_cap - today_cost) if daily_cap > 0 else float("inf")
    monthly_remaining = (monthly_cap - month_cost) if monthly_cap > 0 else float("inf")
    headroom = min(daily_remaining, monthly_remaining)
    if reserved > headroom:
        raise ValueError(
            f"Dream cost estimate ${reserved:.6f} exceeds remaining headroom "
            f"${headroom:.6f}. Use critical=True to bypass."
        )


# ──────────────────────────────────────────────────────────────────
# Main pipeline


def _run_pipeline(
    agent_root: Path,
    dream_dir: Path,
    result: DreamResult,
    journal_lookback_days: int,
    log_lookback_days: int,
    instructions: str,
    model: str,
    critical: bool,
    backend: "MemoryBackend | None" = None,
    log_backend: "LogBackend | None" = None,
    journal_backend: "JournalBackend | None" = None,
) -> DreamResult:
    """Execute the full dream pipeline. Mutates and returns result."""
    from .journal import get_default_journal_backend  # noqa: PLC0415

    total_input_tokens = 0
    total_output_tokens = 0
    total_cost = 0.0

    # Phase 3: Read inputs
    # Route memory reads through the backend protocol when available.
    if backend is not None:
        notes = _read_memory_notes_via_backend(backend)
    else:
        notes = _read_memory_notes(agent_root)

    # ADOPT-NOW (#427 PR1 — spec/43): route journal reads through JournalBackend.
    # backend returns raw JournalEntry objects; adapt to list[dict] for all
    # downstream callers (_build_contradiction_prompts, _build_promotion_prompts,
    # _estimate_dream_cost, _synthesis_pass) which consume list[dict] with
    # keys 'filename' and 'text'. This keeps all 13+ downstream callers unchanged.
    # from_journal_entries in PromotedNote stores path.name (filename only, not
    # full path) — preserve this by using entry.path.name for 'filename'.
    # Upper bound is date.max (NOT date.today()): legacy dream._read_journal_entries
    # applied ONLY a lower bound (if entry_date < cutoff: continue) and had NO
    # upper bound, so future-dated entries (clock skew, post-dated, TZ-boundary
    # writes) were INCLUDED. Passing end=date.today() would silently DROP them —
    # a silent dream-consolidation divergence (#427 PR1 byte-identity selection).
    _jbe = journal_backend or get_default_journal_backend(agent_root)
    cutoff = date.today() - timedelta(days=journal_lookback_days)
    raw_journal_entries = _jbe.query_by_date(start=cutoff, end=date.max)
    journal_entries = [
        # path.name for LLM-facing filename string — full path used for read
        # (fixes #427 subdir-loss latent bug in legacy path.name storage).
        # from_journal_entries holds filename only (path.name), not full path
        # — matches PromotedNote field contract and existing dream report output.
        {"filename": entry.path.name, "text": entry.text}
        for entry in raw_journal_entries
    ]
    log_lines = _read_log_lines(
        agent_root,
        log_lookback_days,
        log_backend=log_backend,
        agent_name=agent_root.name,
    )

    # Update manifest inputs
    result.inputs = DreamInputs(
        memory_count=len(notes),
        journal_lookback_days=journal_lookback_days,
        journal_count=len(journal_entries),
        log_lookback_days=log_lookback_days,
        log_line_count=len(log_lines),
    )
    _write_manifest(dream_dir, result)

    # Phase 4a: Mechanical stale detection (no LLM)
    stale_markings = _detect_stale_notes(notes)

    # Phase 4b: Duplicate detection via helpers
    clusters = _cluster_by_type_and_name(notes)
    dup_prompts, active_clusters = _build_duplicate_prompts(clusters)

    consolidated: list[ConsolidatedNote] = []
    notes_to_consolidate: set[str] = (
        set()
    )  # filenames that are consumed by consolidation

    if dup_prompts:
        dup_results = _batch_llm_calls(dup_prompts, model, max_tokens=512)
        for i, raw_text in enumerate(dup_results):
            total_input_tokens += raw_text.input_tokens
            total_output_tokens += raw_text.output_tokens
            cost, _ = _costs.calc_cost(
                model, raw_text.input_tokens, raw_text.output_tokens
            )
            total_cost += cost
            parsed = _parse_helper_text(raw_text.text)
            if parsed.get("is_duplicate") and parsed.get("merged_body"):
                cluster = active_clusters[i]
                filenames = [n["filename"] for n in cluster]
                merged_name = parsed.get("merged_name") or cluster[0]["meta"].get(
                    "name", "merged"
                )
                consolidated.append(
                    ConsolidatedNote(
                        new=f"consolidated_{len(consolidated) + 1}.md",
                        supersedes=filenames,
                        reason=f"Duplicate notes merged: {', '.join(filenames)}",
                    )
                )
                # Store merged body for synthesis
                cluster[0]["_merged_body"] = parsed["merged_body"]
                cluster[0]["_merged_name"] = merged_name
                for fn in filenames[1:]:
                    notes_to_consolidate.add(fn)

    # Phase 4c: Contradiction detection
    contradiction_prompts = _build_contradiction_prompts(notes, journal_entries)
    if contradiction_prompts:
        contr_results = _batch_llm_calls(contradiction_prompts, model, max_tokens=512)
        for raw_text in contr_results:
            total_input_tokens += raw_text.input_tokens
            total_output_tokens += raw_text.output_tokens
            cost, _ = _costs.calc_cost(
                model, raw_text.input_tokens, raw_text.output_tokens
            )
            total_cost += cost
            # Contradictions inform the synthesis pass via journal content already read

    # Phase 4d: Promotion detection
    promoted: list[PromotedNote] = []
    promo_prompts = _build_promotion_prompts(journal_entries, notes)
    promo_data: list[dict] = []
    if promo_prompts:
        promo_results = _batch_llm_calls(promo_prompts, model, max_tokens=1024)
        for raw_text in promo_results:
            total_input_tokens += raw_text.input_tokens
            total_output_tokens += raw_text.output_tokens
            cost, _ = _costs.calc_cost(
                model, raw_text.input_tokens, raw_text.output_tokens
            )
            total_cost += cost
            parsed = _parse_helper_text(raw_text.text)
            for promo in parsed.get("promotions", []):
                from_entries = promo.get("from_entries", [])
                if not from_entries:
                    continue
                promo_data.append(promo)
                promo_name = promo.get("name", f"promoted_{len(promoted) + 1}")
                from ._schema import derive_filename

                promo_filename = derive_filename(
                    promo.get("type", "feedback"), promo_name
                )
                promoted.append(
                    PromotedNote(
                        new=promo_filename,
                        from_journal_entries=from_entries,
                        reason=f"Recurring journal observation: {promo_name}",
                    )
                )

    # Phase 5: Synthesis pass — one main model call
    today = date.today()
    synthesis_result = _synthesis_pass(
        model=model,
        notes=notes,
        journal_entries=journal_entries,
        log_lines=log_lines,
        stale_markings=stale_markings,
        consolidated_proposals=consolidated,
        promoted_proposals=promoted,
        instructions=instructions,
    )
    total_input_tokens += synthesis_result.input_tokens
    total_output_tokens += synthesis_result.output_tokens
    syn_cost, _ = _costs.calc_cost(
        model, synthesis_result.input_tokens, synthesis_result.output_tokens
    )
    total_cost += syn_cost

    # Parse synthesis output
    synthesis_data = _parse_helper_text(synthesis_result.text)

    # Phase 6: Write outputs via staging (protocol-compliant bulk write area)
    if backend is not None:
        staging = backend.create_staging()
        output_dir = staging.staging_dir
        output_dir.mkdir(parents=True, exist_ok=True)
    else:
        staging = None
        output_dir = dream_dir / "memory"
        output_dir.mkdir(parents=True, exist_ok=True)

    written_notes: list[str] = []
    stale_filenames = {s.note for s in stale_markings}
    stale_lookup = {s.note: s.new_expires_at for s in stale_markings}

    # 6a: Write consolidated notes (with supersedes frontmatter)
    final_consolidated: list[ConsolidatedNote] = []
    for c_proposal in consolidated:
        cluster_filenames = c_proposal.supersedes
        # Find the seed note
        seed = next((n for n in notes if n["filename"] == cluster_filenames[0]), None)
        if seed is None:
            continue
        merged_body = seed.get("_merged_body") or seed["body"]
        merged_name = seed.get("_merged_name") or seed["meta"].get(
            "name", "Merged note"
        )
        meta = dict(seed["meta"])
        meta["name"] = merged_name
        capture = Capture(
            type=meta.get("type", "feedback"),
            name=merged_name,
            description=meta.get("description", "Consolidated note."),
            confidence=meta.get("confidence", "medium"),
            sources=meta.get("sources", ["dream"]),
            body=merged_body,
            supersedes=cluster_filenames[0],
            pinned=bool(meta.get("pinned", False)),
            expires_at=meta.get("expires_at"),
            tags=list(meta.get("tags", [])),
        )
        filename = _write_note_to_dir(
            output_dir, capture, today, supersedes=cluster_filenames
        )
        written_notes.append(filename)
        final_consolidated.append(
            ConsolidatedNote(
                new=filename,
                supersedes=cluster_filenames,
                reason=c_proposal.reason,
            )
        )

    # 6b: Write promoted notes
    final_promoted: list[PromotedNote] = []
    for i, (p_proposal, p_data) in enumerate(zip(promoted, promo_data)):
        valid_type = p_data.get("type", "feedback")
        if valid_type not in {"user", "feedback", "project", "decision", "reference"}:
            valid_type = "feedback"
        p_name = p_data.get("name", f"Promoted observation {i + 1}")
        p_body = p_data.get("body", "Promoted from journal.")
        capture = Capture(
            type=valid_type,
            name=p_name,
            description=p_name[:200],
            confidence="medium",
            sources=p_proposal.from_journal_entries[:5] or ["dream"],
            body=p_body,
        )
        filename = _write_note_to_dir(output_dir, capture, today)
        written_notes.append(filename)
        final_promoted.append(
            PromotedNote(
                new=filename,
                from_journal_entries=p_proposal.from_journal_entries,
                reason=p_proposal.reason,
            )
        )

    # Track which notes have been processed
    consolidated_superseded: set[str] = set()
    for c in final_consolidated:
        consolidated_superseded.update(c.supersedes[1:])  # keep [0] as it's the seed

    # 6c: Copy unchanged notes (stale get updated expires_at)
    for note in notes:
        fn = note["filename"]
        if fn in consolidated_superseded:
            continue  # dropped by consolidation
        # Check if this note was written already as a consolidated note
        from ._schema import derive_filename

        if any(fn in c.supersedes for c in final_consolidated):
            if fn not in [c.supersedes[0] for c in final_consolidated]:
                continue  # secondary in consolidation cluster, skip

        expires = stale_lookup.get(fn) if fn in stale_filenames else None
        _copy_note_to_dir(
            output_dir, fn, note["meta"], note["body"], today, new_expires_at=expires
        )
        if fn not in written_notes:
            written_notes.append(fn)

    # 6d: Build INDEX.md
    all_output_notes = [f for f in written_notes]
    index_content = _build_fresh_index(output_dir, all_output_notes)
    atomic_write(output_dir / "INDEX.md", index_content)

    # 6e: Write report.md
    unchanged_count = (
        len(notes) - len(consolidated_superseded) - len(notes_to_consolidate)
    )
    report_content = _build_report(
        final_consolidated, final_promoted, stale_markings, unchanged_count
    )
    atomic_write(dream_dir / "report.md", report_content)

    # 6f: Move staging area to dream_dir/memory/ for operator review.
    # When backend staging was used, output_dir is inside dreams/.staging-<uuid>/memory/.
    # Rename it to dreams/<id>/memory/ so the operator can review and apply later.
    if staging is not None:
        dream_memory = dream_dir / "memory"
        if not dream_memory.exists():
            os.rename(str(output_dir), str(dream_memory))
        # Mark staging as consumed (it was moved, not applied via backend)
        staging._discarded = True

    # Finalise result
    result.consolidated = final_consolidated
    result.promoted = final_promoted
    result.marked_stale = stale_markings
    result.output_memory_count = len(all_output_notes)
    result.total_input_tokens = total_input_tokens
    result.total_output_tokens = total_output_tokens
    result.total_cost_usd = round(total_cost, 6)
    result.status = "completed"
    result.ended_at = datetime.now().astimezone().isoformat()

    return result


# ──────────────────────────────────────────────────────────────────
# LLM helpers (bypass AtomicAgent persona — meta-task)


class _RawLLMResult:
    """Minimal result from _llm.call_llm."""

    def __init__(self, text: str, input_tokens: int, output_tokens: int):
        self.text = text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


def _single_llm_call(prompt: str, model: str, max_tokens: int = 2048) -> _RawLLMResult:
    """Make one LLM call without agent persona. Returns _RawLLMResult."""
    raw = _llm.call_llm(
        model=model,
        system_prompt=(
            "You are a memory consolidation assistant. "
            "Respond with valid JSON only. No extra commentary."
        ),
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=0.2,
    )
    return _RawLLMResult(
        text=raw.text,
        input_tokens=raw.input_tokens,
        output_tokens=raw.output_tokens,
    )


def _batch_llm_calls(
    prompts: list[str], model: str, max_tokens: int = 512
) -> list[_RawLLMResult]:
    """Run prompts in parallel via threads. Returns results in order."""
    import concurrent.futures

    results: list[Any] = [None] * len(prompts)

    def call_one(idx: int, prompt: str):
        return idx, _single_llm_call(prompt, model, max_tokens=max_tokens)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(call_one, i, p): i for i, p in enumerate(prompts)}
        for future in concurrent.futures.as_completed(futures):
            try:
                idx, res = future.result()
                results[idx] = res
            except Exception:
                idx = futures[future]
                results[idx] = _RawLLMResult(text="{}", input_tokens=0, output_tokens=0)

    return results  # type: ignore


def _synthesis_pass(
    model: str,
    notes: list[dict],
    journal_entries: list[dict],
    log_lines: list[dict],
    stale_markings: list[StaleMarking],
    consolidated_proposals: list[ConsolidatedNote],
    promoted_proposals: list[PromotedNote],
    instructions: str,
) -> _RawLLMResult:
    """One synthesis pass summarising all detection results."""
    notes_summary = "\n".join(
        f"- [{n['filename']}] {n['meta'].get('name', '')} "
        f"(type={n['meta'].get('type', '?')}, "
        f"last_seen={n['meta'].get('last_seen', '?')})"
        for n in notes
    )
    stale_summary = "\n".join(
        f"- {s.note} -> expires {s.new_expires_at}" for s in stale_markings
    )
    consolidated_summary = "\n".join(
        f"- Merge {c.supersedes} -> {c.new}" for c in consolidated_proposals
    )
    promoted_summary = "\n".join(
        f"- Promote from {p.from_journal_entries} -> {p.new}"
        for p in promoted_proposals
    )

    prompt = (
        f"You are reviewing an agent's memory consolidation results.\n\n"
        f"Memory notes ({len(notes)} total):\n{notes_summary}\n\n"
        f"Stale markings ({len(stale_markings)}):\n{stale_summary or '(none)'}\n\n"
        f"Consolidation proposals ({len(consolidated_proposals)}):\n"
        f"{consolidated_summary or '(none)'}\n\n"
        f"Promotion proposals ({len(promoted_proposals)}):\n"
        f"{promoted_summary or '(none)'}\n\n"
        + (f"Operator instructions: {instructions}\n\n" if instructions else "")
        + "Confirm these changes are sound and complete.\n"
        "Respond with JSON: "
        '{"confirmed": true, "notes": "any final observations"}'
    )
    return _single_llm_call(prompt, model, max_tokens=512)


# ──────────────────────────────────────────────────────────────────
# DreamRunner


class DreamRunner:
    """Operator-facing dream pipeline coordinator.

    Usage:
        runner = DreamRunner(agents_root, "my-agent")
        result = runner.start()
        print(runner.review(result.dream_id))
        runner.apply(result.dream_id)
    """

    def __init__(
        self,
        agents_root: Path | str,
        agent_name: str,
        model: str | None = None,
        *,
        dream_lock_timeout: float = 30.0,
        lock_backend: LockBackend | None = None,
        log_backend: "LogBackend | None" = None,
        memory_backend: "MemoryBackend | None" = None,
        profile_backend: "AgentProfileBackend | None" = None,
        tool_registry_backend: "ToolRegistryBackend | None" = None,
        mandate_backend: "MandateBackend | None" = None,
        policy_backend: "PolicyBackend | None" = None,
        persona_backend: "PersonaBackend | None" = None,
        corpus_backend: "CorpusBackend | None" = None,
        mcp_server_registry_backend: "MCPServerRegistryBackend | None" = None,
        journal_backend: "JournalBackend | None" = None,
    ):
        self.agents_root = Path(agents_root)
        self.agent_name = agent_name
        self.agent_root = self.agents_root / agent_name
        self.dreams_dir = self.agent_root / "dreams"
        # Per-instance dream-lock timeout (constructor kwarg, not a class
        # attribute, matching the FilesystemBackend.apply_staging_lock_
        # timeout pattern — Step 9.1 security specialist rejected
        # class-attribute mutation as a process-wide risk).
        self._dream_lock_timeout = dream_lock_timeout

        if not self.agent_root.exists():
            raise AtomicAgentsError(
                f"Agent folder not found: {self.agent_root}. "
                f"Set ATOMIC_AGENTS_ROOT or create the agent."
            )

        # Operator-config resolution: kwarg ALWAYS wins over env vars
        # per spec/21 §"Operator override surface".
        if lock_backend is None:
            agent_lock_backend = get_default_lock_backend(self.agent_root)
        else:
            agent_lock_backend = lock_backend

        # Memory backend — shared across start/apply/discard calls.
        # kwarg-wins: an explicit memory_backend= bypasses env-var resolution.
        # When None, the factory reads ATOMIC_AGENTS_MEMORY_BACKEND and threads
        # agent_lock_backend so apply_staging's lock acquires through the SAME
        # backend instance the agent uses (Step 11 adversarial P0-1: without
        # this, an operator who passes ``DreamRunner(...,
        # lock_backend=RedisLockBackend(...))`` with env vars unset gets a
        # Redis dream lock but a filesystem apply_staging lock — meaningless
        # across hosts, opening a write-data-race on memory/ during dream
        # apply).  See spec/20 §"Operator override surface".
        if memory_backend is None:
            self._backend: MemoryBackend = get_default_memory_backend(
                self.agent_root, lock_backend=agent_lock_backend
            )
        else:
            self._backend = memory_backend

        # PR-1 scope guard (#396): DreamRunner.apply() wraps the on-disk dream
        # output dir as a FilesystemStagedMemory and feeds it to
        # ``self._backend.apply_staging(...)``. That staging-from-an-existing-
        # directory path is filesystem-shaped; a non-filesystem backend cannot
        # consume it. Rather than silently break on apply, fail loud at
        # construction so the limitation is honest. Routing apply() through a
        # backend-agnostic staging adopt path is tracked in #396.
        if not isinstance(self._backend, FilesystemBackend):
            raise NotImplementedError(
                "DreamRunner currently requires the filesystem memory backend "
                "(ATOMIC_AGENTS_MEMORY_BACKEND=filesystem); apply() assumes a "
                "FilesystemStagedMemory staging area. Non-filesystem memory "
                "backends are not yet supported for the dream apply path "
                "(tracked in #396)."
            )

        # Dream lock backend — re-scoped to ``"dreams"`` via the
        # Protocol's ``scope()`` method (#60 PR 3 + spec/21 §"scope()").
        # Filesystem produces ``<dreams_dir>/.lock`` (byte-identical to
        # the legacy ``_DreamLock`` artifact); Redis produces a key
        # under ``<key_prefix>dreams:``. Distinct scope from the agent's
        # main lock — long-standing invariant from spec/16: an
        # in-progress dream does NOT block ``agent.call()`` and vice
        # versa.
        self._dream_lock_backend = agent_lock_backend.scope("dreams")

        # LogBackend for cost reads and log_lines walk (#61 PR 2).
        # Operators may pin the backend via the ``log_backend=`` kwarg
        # OR via ``ATOMIC_AGENTS_LOG_BACKEND`` env var. The kwarg
        # ALWAYS wins. Same forward-pointer rule as the lock backend:
        # PR 2 must thread this through to ``_read_log_lines`` and
        # ``_check_cap`` so dream cost rollups read from the SAME
        # backend ``agent.call()`` writes to — preventing the
        # multi-backend split-brain the lock arc PR 3 Step 11
        # adversarial caught (operator pins Redis lock, DreamRunner
        # silently constructs filesystem; here: operator pins SQLite
        # log backend, dream silently walks empty filesystem).
        if log_backend is None:
            from .logs import get_default_log_backend

            self._log_backend = get_default_log_backend(self.agent_root)
        else:
            self._log_backend = log_backend

        # Profile backend resolution + pre-load the agent's profile once.
        # #63 PR 2 Step 11 P1#3: DreamRunner had TWO model.md call sites
        # (this one at __init__ + the _check_cap cost-guardrail). PR 2
        # routes BOTH through the profile backend so an operator-pinned
        # backend (e.g., SaaS DatabaseAgentProfileBackend) supplies the
        # canonical model_config for both. Same kwarg-wins-over-env-var
        # discipline as lock_backend / log_backend.
        if profile_backend is None:
            from .profile import get_default_profile_backend

            self._profile_backend = get_default_profile_backend(self.agents_root)
        else:
            self._profile_backend = profile_backend
        self._profile = self._profile_backend.load_profile(self.agent_name)

        # #64 PR 2 — ToolRegistryBackend stored for API parity with
        # OutcomeRunner / EvalRunner. DreamRunner currently makes raw
        # LLM calls (``_llm.call_*``) without dispatching agent tools —
        # there is no internal ``AtomicAgent`` construction to thread
        # through. The kwarg + storage exist so an operator wiring
        # multiple runners uses ONE signature shape across all three.
        # Reserved for future dream pipelines that DO dispatch tools
        # (a future ``DreamRunner`` capability that emits agent calls
        # for note synthesis would consume this).
        self._tool_registry_backend = tool_registry_backend
        # #124 PR 2 — MandateBackend stored for API parity with
        # OutcomeRunner / EvalRunner. DreamRunner currently makes raw
        # LLM calls (``_llm.call_*``) without dispatching agent tools
        # — there is no internal ``AtomicAgent`` construction to thread
        # through. The kwarg + storage exist so an operator wiring
        # multiple runners uses ONE signature shape across all four. Per
        # spec/29, mandate validation is per-agent scoped; future dream
        # pipelines that emit agent calls for note synthesis would consume
        # this backend via ``AtomicAgent(..., mandate_backend=...)``.
        self._mandate_backend = mandate_backend
        # #89 PR 2 — PolicyBackend stored for API parity with
        # OutcomeRunner / EvalRunner. DreamRunner currently makes raw
        # LLM calls (``_llm.call_*``) without dispatching agent tools
        # — there is no internal ``AtomicAgent`` construction to thread
        # through in v1. The kwarg + storage exist so an operator wiring
        # multiple runners uses ONE signature shape across all runners.
        # Future dream pipelines that DO construct an internal AtomicAgent
        # (e.g., for self-reflection cycles) will thread the stored backend
        # at that construction site via
        # ``AtomicAgent(..., policy_backend=self._policy_backend)``.
        self._policy_backend = policy_backend
        # #62 PR 2 — PersonaBackend stored for API parity with
        # OutcomeRunner / EvalRunner. DreamRunner currently makes raw
        # LLM calls (``_llm.call_*``) without dispatching agent tools
        # — there is no internal ``AtomicAgent`` construction to thread
        # through in v1. The kwarg + storage exist so an operator wiring
        # multiple runners uses ONE signature shape across all four. Per
        # spec/33 D-ER-2, persona_backend is scoped to agents_root; future
        # dream pipelines that construct an internal AtomicAgent for
        # self-reflection cycles will thread the stored backend at that
        # construction site via
        # ``AtomicAgent(..., persona_backend=self._persona_backend)``.
        self._persona_backend = persona_backend
        # spec/34 PR 3 — CorpusBackend stored for API parity with
        # OutcomeRunner / EvalRunner. DreamRunner currently makes raw
        # LLM calls (``_llm.call_*``) without dispatching agent tools
        # — there is no internal ``AtomicAgent`` construction site to
        # thread through in v1; kwarg exists for API parity with
        # OutcomeRunner and EvalRunner. Future dream pipelines that
        # construct an internal AtomicAgent (e.g., for self-reflection
        # cycles) will thread the stored backend at that construction
        # site via
        # ``AtomicAgent(..., corpus_backend=self._corpus_backend)``.
        self._corpus_backend = corpus_backend
        # spec/36 PR 2 -- MCPServerRegistryBackend stored for API parity with
        # OutcomeRunner / EvalRunner. DreamRunner currently makes raw
        # LLM calls (``_llm.call_*``) without dispatching agent tools
        # -- there is no internal ``AtomicAgent`` construction site to
        # thread through in v1. The kwarg exists so an operator wiring
        # multiple runners uses ONE signature shape across all runners.
        # Future dream pipelines that construct an internal AtomicAgent
        # (e.g., for self-reflection cycles) will thread the stored backend
        # at that construction site via
        # ``AtomicAgent(..., mcp_server_registry_backend=self._mcp_server_registry_backend)``.
        self._mcp_server_registry_backend = mcp_server_registry_backend
        # spec/43 PR 1 — JournalBackend LIVE-WIRED (ADOPT-NOW ruling).
        # Unlike the other backends stored above for API-parity only, this one
        # IS consumed by _run_pipeline() and start() to replace _read_journal_entries.
        # DreamRunner is the only consumer of query_by_date() (date-window read).
        self._journal_backend = journal_backend

        # Resolve model: explicit kwarg > profile.model_config default.
        # PR 2 Decision 2: pre-resolved model_config is also passed to
        # _check_cap in start() so the cost-guardrail uses the same
        # model_config — no second profile load, no second model.md read.
        if model:
            self._model = model
        else:
            self._model = self._profile.model_config["default_model"]

    def start(
        self,
        journal_lookback_days: int = 30,
        log_lookback_days: int = 30,
        instructions: str = "",
        critical: bool = False,
    ) -> DreamResult:
        """Run the consolidation pipeline synchronously. Returns completed DreamResult."""
        dream_id = _new_dream_id()
        dream_dir = self.dreams_dir / dream_id
        dream_dir.mkdir(parents=True, exist_ok=True)

        # Upfront cost estimate and cap check
        # ADOPT-NOW (#427 PR1 — spec/43): route journal reads through JournalBackend.
        # Adapt JournalEntry → list[dict] for _estimate_dream_cost (list[dict] consumer).
        # This cost-estimate read happens BEFORE _check_cap so TypeError from JournalEntry
        # would abort before the cost gate — using the same adapter as _run_pipeline
        # preserves the cost gate ordering (spec/43 prep finding P1 at dream.py:685).
        from .journal import get_default_journal_backend as _get_jbe  # noqa: PLC0415

        notes = _read_memory_notes(self.agent_root)
        # end=date.max mirrors legacy's lower-bound-only filter (see _run_pipeline);
        # date.today() would drop future-dated entries the legacy path included.
        _jbe_start = self._journal_backend or _get_jbe(self.agent_root)
        _cutoff_start = date.today() - timedelta(days=journal_lookback_days)
        _raw_je = _jbe_start.query_by_date(start=_cutoff_start, end=date.max)
        journal_entries = [{"filename": e.path.name, "text": e.text} for e in _raw_je]
        log_lines = _read_log_lines(
            self.agent_root,
            log_lookback_days,
            log_backend=self._log_backend,
            agent_name=self.agent_name,
        )
        # NOTE (issue #497): a degraded log read (LogBackendReadError) makes
        # _read_log_lines return [] (see its except branch), so log_chars=0 and
        # this upfront estimate is conservative-LOW. That cannot leak uncosted
        # spend, covering both cases (cf. the #495 "fail-closed only where there
        # is something to protect" lesson): WHEN A CAP IS SET, the actual-spend
        # degraded branch in _check_cap (the _sum_via_backend fail-closed path)
        # is the binding gate on a blind read and fires first, so the estimate
        # undercount is moot in exactly the failure case; WHEN NO CAP IS SET,
        # _check_cap does not block, but an uncapped agent has no budget to leak
        # against regardless, so the undercount is harmless.
        estimated_cost = _estimate_dream_cost(
            self._model, notes, journal_entries, log_lines
        )
        _check_cap(
            self.agent_root,
            self._model,
            estimated_cost,
            critical,
            log_backend=self._log_backend,
            agent_name=self.agent_name,
            # #63 PR 2 Decision 2: pass the pre-resolved model_config
            # from the profile_backend so _check_cap doesn't re-read
            # model.md from disk. Step 11 P1#3 from PR 1 named this as
            # the load-bearing fix — without it, an operator using a
            # non-filesystem profile_backend would have correct config
            # for AtomicAgent.call() but stale cost caps applied to
            # dream runs (the dream cost-guardrail would silently fall
            # back to filesystem model.md, which may be absent or
            # diverge from the operator's pinned source).
            model_config=self._profile.model_config,
        )

        # Initialise manifest
        result = DreamResult(
            dream_id=dream_id,
            agent_name=self.agent_name,
            status="pending",
            model=self._model,
            instructions=instructions,
            inputs=DreamInputs(
                memory_count=len(notes),
                journal_lookback_days=journal_lookback_days,
                journal_count=len(journal_entries),
                log_lookback_days=log_lookback_days,
                log_line_count=len(log_lines),
            ),
            output_memory_count=0,
            consolidated=[],
            promoted=[],
            marked_stale=[],
            total_input_tokens=0,
            total_output_tokens=0,
            total_cost_usd=0.0,
            started_at=datetime.now().astimezone().isoformat(),
            ended_at=None,
            error=None,
        )
        _write_manifest(dream_dir, result)

        # Acquire dream lock via the bound LockBackend. ``LockBusy`` is
        # wrapped in ``DreamInProgress`` (with PEP-3134 ``from exc``
        # exception chaining for debug traceback) so operators catching
        # the domain exception see the same surface as pre-PR 2.
        try:
            lock_handle = self._dream_lock_backend.acquire(
                "", timeout=self._dream_lock_timeout
            )
        except LockBusy as exc:
            raise DreamInProgress(
                f"Dream lock at {self.dreams_dir / '.lock'} is held; "
                f"another dream is in progress."
            ) from exc

        result.status = "running"
        _write_manifest(dream_dir, result)

        try:
            result = _run_pipeline(
                agent_root=self.agent_root,
                dream_dir=dream_dir,
                result=result,
                journal_lookback_days=journal_lookback_days,
                log_lookback_days=log_lookback_days,
                instructions=instructions,
                model=self._model,
                critical=critical,
                backend=self._backend,
                log_backend=self._log_backend,
                journal_backend=self._journal_backend,
            )
            _write_manifest(dream_dir, result)
            # Inter-pipeline lock-loss check (#60 PR 3 + spec/21
            # §"Lease and heartbeat"). For lease-backed backends, the
            # heartbeat thread may have detected lease expiry during
            # the pipeline. Surface as ``LockLost`` BEFORE marking the
            # dream as completed so a dream run that lost its lease
            # mid-flight aborts instead of writing a completed manifest
            # under a lock another holder now owns. No-op for the
            # filesystem default. Step 9.1 maintainability specialist
            # flagged the unwired ``check_lock_lost`` import.
            check_lock_lost(lock_handle)
        except Exception as exc:
            result.status = "failed"
            result.error = str(exc)
            result.ended_at = datetime.now().astimezone().isoformat()
            _write_manifest(dream_dir, result)
            raise
        finally:
            # Single release-on-exit covers both the success and the
            # failure paths. The legacy ``_DreamLock``-era code called
            # release() in both the ``except`` AND the ``finally`` and
            # relied on FilesystemLockBackend.release() being idempotent
            # (spec/21 §"release(handle)"). That worked but depended on
            # idempotency where it was avoidable — Step 9.1 testing
            # specialist flagged the pattern as a footgun if a future
            # backend's release() is not idempotent. The single-release
            # finally is the safer shape (CLAUDE.md rule #8 — "no
            # half-finished state").
            self._dream_lock_backend.release(lock_handle)

        return result

    def status(self, dream_id: str | None = None) -> DreamResult:
        """Read manifest for a specific dream, or the most recent if dream_id is None."""
        if dream_id:
            dream_dir = self.dreams_dir / dream_id
            if not dream_dir.exists():
                raise DreamNotFound(
                    f"Dream {dream_id!r} not found for agent {self.agent_name!r}"
                )
            return _read_manifest(dream_dir)
        # Most recent
        dreams = self.list_dreams()
        if not dreams:
            raise DreamNotFound(f"No dreams found for agent {self.agent_name!r}")
        return dreams[0]

    def review(self, dream_id: str) -> str:
        """Return the contents of <dream-dir>/report.md."""
        dream_dir = self.dreams_dir / dream_id
        if not dream_dir.exists():
            raise DreamNotFound(
                f"Dream {dream_id!r} not found for agent {self.agent_name!r}"
            )
        report_path = dream_dir / "report.md"
        if not report_path.exists():
            raise AtomicAgentsError(
                f"No report.md in dream {dream_id!r} — pipeline may not be complete"
            )
        return report_path.read_text(encoding="utf-8")

    def apply(self, dream_id: str) -> Path:
        """Atomically swap memory/ ↔ dreams/<id>/memory/.

        1. Refuse if dream status != 'completed'
        2. Refuse if already applied
        3. Rename current <agent>/memory/ → <agent>/memory.archived-<ts>/
        4. Rename <agent>/dreams/<id>/memory/ → <agent>/memory/
        5. Update manifest with applied_at + archived_path
        Returns the path of the archived memory dir (for revert).
        """
        dream_dir = self.dreams_dir / dream_id
        if not dream_dir.exists():
            raise DreamNotFound(f"Dream {dream_id!r} not found")

        result = _read_manifest(dream_dir)

        if result.status != "completed":
            raise AtomicAgentsError(
                f"Cannot apply dream {dream_id!r}: status is {result.status!r} (need 'completed')"
            )
        if result.applied_at:
            raise AtomicAgentsError(
                f"Dream {dream_id!r} was already applied at {result.applied_at}"
            )

        dreamed_memory = dream_dir / "memory"
        if not dreamed_memory.exists():
            raise AtomicAgentsError(
                f"Dreamed memory/ dir not found at {dreamed_memory} — dream output is incomplete"
            )

        # Build a permissive write policy allowing the agent's memory dir.
        # apply_staging() handles the agent lock (via FilesystemBackend.
        # _lock_backend per #60 PR 2) + atomic rename internally.
        memory_dir = self.agent_root / "memory"
        policy = WritePolicy(write_paths=[memory_dir, self.agent_root])

        # Wrap the dream output dir as a FilesystemStagedMemory so apply_staging
        # can do the lock-aware atomic swap through the backend protocol.
        staged = FilesystemStagedMemory(
            backend_id=dream_id,
            staging_dir=dreamed_memory,
        )

        # apply_staging archives current memory to memory.archived-<ts>, promotes
        # dreamed_memory to memory/, acquires the agent lock internally (via
        # FilesystemBackend._lock_backend per #60 PR 2).
        self._backend.apply_staging(staged, policy)

        # Retrieve the archived path from disk (apply_staging uses its own ts)
        # by scanning for the newest memory.archived-* dir.
        archived = self._find_archived_memory()

        # Step 5: update manifest
        result.applied_at = datetime.now().astimezone().isoformat()
        result.archived_path = str(archived) if archived else None
        _write_manifest(dream_dir, result)

        return archived

    def _find_archived_memory(self) -> Path | None:
        """Return the most recently created memory.archived-* directory, or None."""
        candidates = sorted(
            self.agent_root.glob("memory.archived-*"),
            key=lambda p: p.name,
            reverse=True,
        )
        return candidates[0] if candidates else None

    def discard(self, dream_id: str) -> None:
        """Remove the dreamed output dir. Refuses if already applied."""
        # Validate dream_id before constructing a path from it.
        # dream_id must contain only alphanumerics, underscores, and hyphens —
        # no path separators.  This prevents "../../persona" style traversal.
        if not _VALID_DREAM_ID_RE.match(dream_id):
            raise DreamNotFound(
                f"Invalid dream_id {dream_id!r}: must contain only alphanumeric "
                "characters, underscores, and hyphens"
            )
        dream_dir = self.dreams_dir / dream_id
        # Belt-and-suspenders: also verify the resolved path is inside dreams_dir
        try:
            dream_dir.resolve().relative_to(self.dreams_dir.resolve())
        except ValueError:
            raise DreamNotFound(
                f"Dream path for {dream_id!r} resolves outside the dreams directory"
            )
        if not dream_dir.exists():
            raise DreamNotFound(f"Dream {dream_id!r} not found")

        result = _read_manifest(dream_dir)
        if result.applied_at:
            raise AtomicAgentsError(
                f"Cannot discard dream {dream_id!r} — it was already applied at {result.applied_at}. "
                f"To revert, rename {result.archived_path!r} back to memory/."
            )

        shutil.rmtree(str(dream_dir))

    def list_dreams(self) -> list[DreamResult]:
        """All dreams for this agent, newest first."""
        if not self.dreams_dir.exists():
            return []
        results = []
        for entry in self.dreams_dir.iterdir():
            if not entry.is_dir():
                continue
            if not entry.name.startswith("drm_"):
                continue
            try:
                r = _read_manifest(entry)
                results.append(r)
            except (DreamNotFound, Exception):
                continue
        results.sort(key=lambda r: r.started_at, reverse=True)
        return results


# ──────────────────────────────────────────────────────────────────
# CLI


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="atomic-agents.dream",
        description="Memory consolidation pipeline — dream between sessions",
    )
    parser.add_argument("agent", help="agent name (folder under agents-root)")
    parser.add_argument(
        "--status",
        nargs="?",
        const=True,
        default=None,
        metavar="DREAM_ID",
        help="show status of DREAM_ID, or most recent if omitted",
    )
    parser.add_argument(
        "--review", metavar="DREAM_ID", help="print report.md for DREAM_ID"
    )
    parser.add_argument(
        "--apply", metavar="DREAM_ID", help="atomically apply DREAM_ID to memory/"
    )
    parser.add_argument(
        "--discard", metavar="DREAM_ID", help="remove DREAM_ID output dir"
    )
    parser.add_argument(
        "--list", action="store_true", help="list all dreams for this agent"
    )
    parser.add_argument(
        "--instructions", default="", help="operator hint for the synthesis pass"
    )
    parser.add_argument(
        "--journal-lookback",
        type=int,
        default=30,
        metavar="DAYS",
        help="days of journal to include",
    )
    parser.add_argument(
        "--log-lookback",
        type=int,
        default=30,
        metavar="DAYS",
        help="days of log to include",
    )
    parser.add_argument("--critical", action="store_true", help="bypass cost cap")
    parser.add_argument("--model", default=None, help="override model id")
    parser.add_argument(
        "--agents-root", default=None, help="override ATOMIC_AGENTS_ROOT"
    )

    args = parser.parse_args(argv)

    agents_root = (
        Path(args.agents_root).expanduser().resolve()
        if args.agents_root
        else get_agents_root()
    )

    try:
        runner = DreamRunner(agents_root, args.agent, model=args.model)
    except AtomicAgentsError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if args.list:
        dreams = runner.list_dreams()
        if not dreams:
            print(f"No dreams for agent '{args.agent}'.")
            return 0
        for d in dreams:
            applied = " [applied]" if d.applied_at else ""
            print(f"{d.dream_id}  {d.status}  {d.started_at}{applied}")
        return 0

    if args.status is not None:
        dream_id = args.status if args.status is not True else None
        try:
            result = runner.status(dream_id)
        except (DreamNotFound, AtomicAgentsError) as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        print(json.dumps(_manifest_to_dict(result), indent=2, default=str))
        return 0 if result.status in ("completed", "pending", "running") else 1

    if args.review:
        try:
            print(runner.review(args.review))
        except (DreamNotFound, AtomicAgentsError) as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        return 0

    if args.apply:
        try:
            archived = runner.apply(args.apply)
            print(f"Applied. Previous memory archived to: {archived}")
        except (DreamNotFound, AtomicAgentsError) as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        return 0

    if args.discard:
        try:
            runner.discard(args.discard)
            print(f"Dream {args.discard} discarded.")
        except (DreamNotFound, AtomicAgentsError) as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        return 0

    # Default: start a dream
    try:
        result = runner.start(
            journal_lookback_days=args.journal_lookback,
            log_lookback_days=args.log_lookback,
            instructions=args.instructions,
            critical=args.critical,
        )
    except ValueError as e:
        # Cost guardrail blocked
        print(f"Cost guardrail: {e}", file=sys.stderr)
        return 2
    except DreamInProgress as e:
        print(f"Dream in progress: {e}", file=sys.stderr)
        return 1
    except AtomicAgentsError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(f"Dream completed: {result.dream_id}")
    print(f"  Consolidated:  {len(result.consolidated)}")
    print(f"  Promoted:      {len(result.promoted)}")
    print(f"  Marked stale:  {len(result.marked_stale)}")
    print(f"  Output notes:  {result.output_memory_count}")
    print(f"  Cost:          ${result.total_cost_usd:.6f}")
    print(
        f"\nTo review:  python -m atomic_agents.dream {args.agent} --review {result.dream_id}"
    )
    print(
        f"To apply:   python -m atomic_agents.dream {args.agent} --apply {result.dream_id}"
    )
    print(
        f"To discard: python -m atomic_agents.dream {args.agent} --discard {result.dream_id}"
    )

    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    import sys

    sys.exit(main())

"""Shared per-type renderer — turns typed canonical objects into on-disk bytes.

Two renderer classes live here, with different byte sinks (spec/40 §"Renderer
module"):

(a) Byte-paired passthrough + RunRecord renderers
    (``render_note_bytes_from_raw``, ``render_corpus_page_bytes_from_raw``,
    ``render_run_record_bytes``) — consumed by the filesystem export impls in
    PRODUCTION (``export_memory`` / ``export_corpus`` / ``export_log`` in
    ``export/filesystem.py`` call them directly). These renderers are the
    designated insertion point for future read-path sharing: once the read-paths
    (_path_to_note, read_page) are wired through these functions, the read-path
    and export-path will share the same byte producer and cannot structurally
    disagree. TODAY the read-paths parse independently and do NOT call these
    renderers — the structural guarantee is forward-looking, not yet in force.

(b) Object-graph renderers (``render_note_bytes_from_object``,
    ``render_corpus_page_bytes_from_object``, ``render_mandates_md``,
    ``render_secret_export_bytes``) — for the types where the typed
    ``ExportableResult`` IS the export and byte-rendering is the consumer's job.
    ``export_mandate`` / ``export_secret`` return typed objects and do NOT call
    a renderer; these are consumed by export consumers (the #430 CLI, the
    conformance suite) and by future Tier B (structured-DB) backends that have
    no raw bytes to pass through.

CRITICAL SERIALIZATION RULES (spec/40 MUST 8):
- RunRecord bytes MUST be produced by ``json.dumps(record.to_dict())`` —
  NOT ``atomic_agents._canonical.canonical_json()``. canonical_json sorts
  keys alphabetically for stable hashing; the export renderer MUST preserve
  the ts-first insertion order that to_dict() produces. Using canonical_json
  here would silently break Tier A byte-exact round-trip for every log record.
- Note/CorpusPage bytes: raw file bytes are passed through directly (read by
  the filesystem export impl). The renderer accepts pre-computed bytes for
  Tier A backends and falls through to frontmatter.dumps() only for Tier B.

Tier A (filesystem/raw-fidelity) MUST round-trip BYTE-FOR-BYTE.
Tier B (structured DB) MUST round-trip every dispatch-relevant/core field
losslessly + document comment/formatting loss.

Caveats (spec/40 §"Tier A vs Tier B fidelity"):
- BYTE-FOR-BYTE fidelity holds for notes without extra_frontmatter.
  Notes with operator-added custom frontmatter keys exhibit key-ordering
  divergence in extra_frontmatter on re-render via frontmatter.dumps().
  Tier A conformance tests MUST use fixtures without extra_frontmatter.
- BYTE-FOR-BYTE fidelity applies to records written via LogBackend Protocol.
  Pre-Protocol agent._log() dict records have pre-existing key-order
  differences and are Tier B equivalent.

See docs/spec/40-canonical-export.md §"Renderer module" for the
full normative text.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..corpus.types import CorpusPage
    from ..logs.types import RunRecord
    from ..mandate.types import Mandate
    from ..memory.backend import Note


# ──────────────────────────────────────────────────────────────────────────────
# RunRecord renderer
# MUST use json.dumps(record.to_dict()) — NOT canonical_json().
# See module docstring for the critical warning.


def render_run_record_bytes(record: "RunRecord") -> bytes:
    """Render a RunRecord to the exact bytes FilesystemLogBackend.append() writes.

    Uses ``json.dumps(record.to_dict())`` with NO sort_keys and NO compact
    separators. This preserves the ts-first insertion order from to_dict()
    and matches the byte shape on disk.

    NOT ``canonical_json()`` — sort_keys would break the Tier A byte-exact
    round-trip (spec/40 MUST 8 / MUST NOT).

    Returns UTF-8 encoded bytes with a trailing newline, matching
    ``_io.atomic_append_jsonl``.
    """
    # json.dumps with default separators (", ", ": ") — exactly matching
    # what atomic_append_jsonl writes. No sort_keys.
    line = json.dumps(record.to_dict())
    return (line + "\n").encode("utf-8")


# ──────────────────────────────────────────────────────────────────────────────
# Note renderer
# For Tier A (filesystem): raw bytes are passed through directly.
# For Tier B: re-serialize through frontmatter.dumps().
# Note: frontmatter.dumps() sorts ALL keys alphabetically, which means
# notes with extra_frontmatter exhibit key-ordering divergence.


def render_note_bytes_from_raw(raw_bytes: bytes) -> bytes:
    """Pass through raw file bytes for Tier A byte-exact fidelity.

    This is the Tier A path: the filesystem export impl reads the raw file
    bytes and passes them here. No re-serialization happens, so the round-trip
    is byte-exact regardless of date formatting or extra_frontmatter key order.

    Args:
        raw_bytes: bytes read directly from the note file on disk.

    Returns:
        The same bytes, unchanged.
    """
    return raw_bytes


def render_note_bytes_from_object(note: "Note") -> bytes:
    """Re-serialize a Note object to markdown bytes (Tier B path).

    Used by structured backends (Postgres, SQLite) that do not have access
    to the original raw bytes. Produces frontmatter-formatted markdown.

    CAVEAT: This path does NOT guarantee byte-exact Tier A fidelity.
    - date objects serialize as unquoted YAML (2026-06-01 not '2026-06-01').
    - extra_frontmatter keys are sorted alphabetically by frontmatter.dumps().
    - These divergences are documented in spec/40 §"Tier A vs Tier B fidelity".

    Tier B backends MUST document these losses in their module docstring.
    """
    import frontmatter as fm

    from ..memory.backend import Note

    if not isinstance(note, Note):
        raise TypeError(f"Expected Note, got {type(note)}")

    meta: dict[str, Any] = {
        "schema_version": note.schema_version,
        "name": note.name,
        "description": note.description,
        "type": note.type,
        "captured": note.captured.isoformat() if note.captured else None,
        "last_seen": note.last_seen.isoformat() if note.last_seen else None,
        "sources": note.sources,
        "confidence": note.confidence,
    }
    if note.pinned:
        meta["pinned"] = True
    if note.expires_at:
        meta["expires_at"] = note.expires_at
    if note.supersedes:
        meta["supersedes"] = note.supersedes
    if note.tags:
        meta["tags"] = note.tags
    if note.archived:
        meta["archived"] = note.archived
    if note.superseded_by:
        meta["superseded_by"] = note.superseded_by
    # Merge in extra_frontmatter (operator-added custom fields)
    meta.update(note.extra_frontmatter)

    post = fm.Post(note.body, **meta)
    return (fm.dumps(post) + "\n").encode("utf-8")


# ──────────────────────────────────────────────────────────────────────────────
# CorpusPage renderer
# Same two-tier approach as Note.


def render_corpus_page_bytes_from_raw(raw_bytes: bytes) -> bytes:
    """Pass through raw file bytes for Tier A byte-exact fidelity (CorpusPage)."""
    return raw_bytes


def render_corpus_page_bytes_from_object(page: "CorpusPage") -> bytes:
    """Re-serialize a CorpusPage object to markdown bytes (Tier B path).

    Same caveats as render_note_bytes_from_object regarding date formatting
    and key ordering.
    """
    import frontmatter as fm

    from ..corpus.types import CorpusPage

    if not isinstance(page, CorpusPage):
        raise TypeError(f"Expected CorpusPage, got {type(page)}")

    meta: dict[str, Any] = {}
    if page.name:
        meta["name"] = page.name
    if page.description:
        meta["description"] = page.description
    if page.type:
        meta["type"] = page.type
    if page.captured:
        meta["captured"] = page.captured.isoformat()
    if page.last_seen:
        meta["last_seen"] = page.last_seen.isoformat()
    if page.sources:
        meta["sources"] = page.sources
    if page.provenance:
        meta["provenance"] = page.provenance
    if page.confidence:
        meta["confidence"] = page.confidence
    if page.pinned:
        meta["pinned"] = True
    if page.related:
        meta["related"] = page.related
    if page.tags:
        meta["tags"] = page.tags
    if page.schema_version is not None:
        meta["schema_version"] = page.schema_version
    if page.expires_at:
        meta["expires_at"] = page.expires_at.isoformat()
    if page.supersedes:
        meta["supersedes"] = page.supersedes
    if page.superseded_by:
        meta["superseded_by"] = page.superseded_by

    post = fm.Post(page.body, **meta)
    return (fm.dumps(post) + "\n").encode("utf-8")


# ──────────────────────────────────────────────────────────────────────────────
# Mandate renderer (object-graph class — see module docstring (b))
# Converts list[Mandate] (+ optional _meta) back to mandates.md-format text.
# This is NOT consumed by export_mandate() (which returns typed Mandate objects);
# it is the byte-rendering primitive the #430 CLI, the conformance suite, and a
# future Tier B MandateBackend call downstream of export().


def render_mandates_md(mandates: list["Mandate"], meta: "Any | None" = None) -> str:
    """Render Mandate objects (+ optional ``_meta`` block) to mandates.md text.

    Produces output that round-trips through the ``mandates_md`` parser:
    ``render_mandates_md(mandates, meta) -> text -> parse_mandates_md(text) ->
    (meta, mandates)`` reproduces the mandate_id, prose scope, constraints,
    granted_by, revocation_state values AND the project-root ``_meta`` policy.

    The parser (``mandates_md._REQUIRED_FIELDS``) requires ``granted_by``,
    ``granted_at``, ``scope``, and ``revocation_state`` UNCONDITIONALLY, so this
    renderer always emits all four — ``scope`` from ``Mandate.prose_scope`` (the
    human-readable authority description retained at parse time) and
    ``revocation_state`` even for the ``active`` value.

    When ``meta`` is a ``ProjectMandateMeta`` (a project-root ``## _meta`` policy
    block) it is emitted as a leading ``## _meta`` section so the
    ``per_agent_mandate_policy`` + ``allowed_per_agent_ids`` security boundary
    survives the round-trip. Re-parsing the rendered text with
    ``is_project_root=True`` reconstructs the same ``ProjectMandateMeta``.
    Dropping it would silently revert a ``forbidden`` policy to the ``open``
    default (spec/40 Round-2 finding).

    The backend-local ``source_path`` is deliberately NOT emitted: it is a
    deployment-specific absolute path (a backend-resolution detail), not part of
    the authored, portable mandates.md shape — emitting it would defeat the
    deployment-agnostic round-trip the spec/40 export contract exists to provide
    (same portability rule SecretExportRef enforces against source strings).

    NOTE: ``revocable_by`` and the per-mandate ``source_hash`` are NOT preserved
    across this round-trip. ``revocable_by`` is parsed but not stored on the
    ``Mandate`` dataclass (it resets to the ``operator`` default on re-parse), and
    ``source_hash`` is recomputed over the re-rendered canonical text (a re-import
    therefore sees a fresh hash, which a deployment enforcing spec/29's
    suspicious-rebind throttle will treat as a new binding). Both are documented
    in spec/40 §"MandateBackend export contract" as known Tier-A-not-byte-exact
    gaps.

    Consumed by export consumers (the #430 CLI and the conformance suite) and by
    future Tier B MandateBackend impls (Postgres, etc.) to reconstruct the
    on-disk mandates.md shape. ``FilesystemMandateBackend.export()`` itself
    returns the typed ``MandateExport`` (objects), NOT bytes — byte rendering
    happens downstream of ``export()``.
    """
    import yaml

    if not mandates and meta is None:
        return ""

    lines: list[str] = []

    # Emit the project-root _meta policy block first when present, so a
    # re-parse with is_project_root=True reconstructs the same ProjectMandateMeta.
    if meta is not None:
        lines.append("## _meta")
        lines.append("")
        meta_parts: dict[str, Any] = {
            "per_agent_mandate_policy": meta.per_agent_mandate_policy,
        }
        if meta.allowed_per_agent_ids:
            meta_parts["allowed_per_agent_ids"] = sorted(meta.allowed_per_agent_ids)
        meta_yaml = yaml.dump(meta_parts, default_flow_style=False, allow_unicode=True)
        lines.append(meta_yaml.rstrip())
        lines.append("")
        lines.append("")

    for mandate in mandates:
        lines.append(f"## {mandate.mandate_id}")
        lines.append("")

        # Emit YAML body matching the mandates.md spec format. ``scope`` and
        # ``revocation_state`` are parser-required (mandates_md._REQUIRED_FIELDS)
        # and MUST always be present for the rendered text to re-parse.
        body_parts: dict[str, Any] = {
            "granted_by": mandate.granted_by,
            "granted_at": mandate.granted_at.isoformat(),
            # prose_scope may be None for mandates constructed without a source
            # mandates.md (e.g. programmatic Mandate(...) instances). Emit an
            # empty string in that case so the rendered text still re-parses;
            # the parser only requires the field to be present.
            "scope": mandate.prose_scope if mandate.prose_scope is not None else "",
        }
        if mandate.expires_at is not None:
            body_parts["expires_at"] = mandate.expires_at.isoformat()
        # Always emit revocation_state — the parser requires it unconditionally.
        # ``expired`` is a DERIVED state (framework infers it from expires_at vs.
        # current time); it is never an operator-authored value and the parser
        # rejects it (mandates_md._parse_revocation_state). list_mandates() only
        # ever returns the authored active/revoked value, but guard defensively
        # so a derived-EXPIRED Mandate still renders to re-parseable text:
        # render it as the authored ``active`` value it was loaded from on disk.
        rev_value = mandate.revocation_state.value
        if rev_value == "expired":
            rev_value = "active"
        body_parts["revocation_state"] = rev_value
        if mandate.revoked_at is not None:
            body_parts["revoked_at"] = mandate.revoked_at.isoformat()
        if mandate.revoked_by is not None:
            body_parts["revoked_by"] = mandate.revoked_by
        if mandate.revocation_reason is not None:
            body_parts["revocation_reason"] = mandate.revocation_reason

        # Constraints
        c = mandate.constraints
        if c.unconstrained:
            body_parts["constraints"] = {
                "unconstrained": True,
                "unconstrained_justification": c.unconstrained_justification or "",
            }
        else:
            constraints: dict[str, Any] = {}
            if c.allowed_tools:
                constraints["allowed_tools"] = sorted(c.allowed_tools)
            if c.allowed_targets:
                # Emit the canonical {kind, value} shape that _parse_target_list
                # accepts.  The previous {pattern, kind} shape was silently
                # rejected by the parser (spec/40 round-trip bug fix).
                constraints["allowed_targets"] = [
                    {"kind": t.kind, "value": t.pattern} for t in c.allowed_targets
                ]
            if c.blocked_targets:
                constraints["blocked_targets"] = [
                    {"kind": t.kind, "value": t.pattern} for t in c.blocked_targets
                ]
            if c.time_window is not None:
                # Emit the {start, end} keys that _parse_time_window expects.
                # The previous {start_utc, end_utc} shape was rejected by the
                # parser (spec/40 round-trip bug fix).
                # Use %H:%M:%S so seconds are preserved: TimeWindow.start_utc /
                # end_utc are datetime.time objects that carry seconds, and the
                # parser _parse_time_of_day accepts "HH:MM:SS". Emitting only
                # %H:%M silently truncates seconds — a mandate with
                # start=09:00:30 would round-trip to 09:00:00, shifting a
                # security constraint by up to 59 s (spec/40 F2 bug fix).
                constraints["time_window"] = {
                    "start": c.time_window.start_utc.strftime("%H:%M:%S"),
                    "end": c.time_window.end_utc.strftime("%H:%M:%S"),
                }
            for field_name in (
                "daily_token_usd",
                "monthly_token_usd",
                "cumulative_token_usd",
                "daily_external_usd",
                "monthly_external_usd",
                "cumulative_external_usd",
                "requires_escalation_above_token_usd",
                "requires_escalation_above_external_usd",
            ):
                val = getattr(c, field_name)
                if val is not None:
                    constraints[field_name] = val
            if c.action_class.value != "external_side_effect":
                constraints["action_class"] = c.action_class.value
            if constraints:
                body_parts["constraints"] = constraints

        body_yaml = yaml.dump(body_parts, default_flow_style=False, allow_unicode=True)
        lines.append(body_yaml.rstrip())
        lines.append("")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ──────────────────────────────────────────────────────────────────────────────
# SecretExportRef renderer — for conformance / portability


def render_secret_export_bytes(entries: list[Any]) -> bytes:
    """Render a list of SecretExportRef objects to portable JSON bytes.

    Produces a JSON array of objects with ``logical_key``, ``hint``,
    and ``present`` fields. NEVER includes resolved plaintext values.

    The conformance test MUST assert that the rendered bytes contain no
    plaintext credential values (spec/40 MUST 9 / never-leak invariant).
    """
    from .types import SecretExportRef

    items = []
    for entry in entries:
        if not isinstance(entry, SecretExportRef):
            raise TypeError(f"Expected SecretExportRef, got {type(entry)}")
        items.append(
            {
                "logical_key": entry.logical_key,
                "hint": entry.hint,
                "present": entry.present,
            }
        )
    return (json.dumps(items, indent=2) + "\n").encode("utf-8")

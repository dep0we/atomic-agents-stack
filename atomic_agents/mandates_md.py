"""Parse ``mandates.md`` operator config for the mandate layer (spec/29).

``mandates.md`` is the operator's durable, written grant of scoped authority
to an agent. The framework reads it; the framework never writes to it (mandate
authorship belongs exclusively to the operator).

File shapes:

- **Per-agent**: ``<agent>/mandates.md`` — mandates that apply only to that
  agent. The ``_meta`` section is invalid here; the parser emits a
  ``logging.warning`` and skips it. The doctor surfaces the misplacement via
  ``check_mandate_meta_misplaced``.
- **Project-root**: ``<project>/mandates.md`` — mandates that apply to all
  agents in the project. May contain a ``## _meta`` section with
  ``per_agent_mandate_policy`` and ``allowed_per_agent_ids``.

Embedded-YAML shape: each ``## <mandate-id>`` section body is parsed as YAML
(matching the embedded-YAML convention from ``model.md`` and ``judges.md``).
The section body begins after the heading line and runs until the next ``## ``
heading or EOF.

Parser contract (spec/29 §"Parser rules"):

- File missing → returns ``(None, [])``. Caller decides what to do.
- File empty → returns ``(None, [])``.
- Any malformed section → raises ``MandateInvalid`` for the **whole file**.
  No partial parse — the operator receives one loud alert listing all errors.
- Duplicate mandate IDs → ``MandateInvalid``.
- ``_meta`` in a per-agent file → ``logging.warning``; section silently
  skipped; doctor surfaces later.
- Mandate ID must match ``[a-z0-9][a-z0-9-]*``, max 64 characters;
  otherwise ``MandateInvalid``.
- Constraint enforceability: at least one structured enforcement field
  required unless ``constraints.unconstrained: true`` + non-empty
  ``unconstrained_justification``.
- ``source_hash``: SHA-256 of the canonical section bytes (text between
  ``## <id>`` and next ``## `` or EOF, stripped, with ``\\n`` line endings).
  Used by ``MandateCheck`` step 2 (TOCTOU defence).

PR 1 of #124. Parser only — returns ``Mandate`` dataclass instances.
``MandateCheck`` (PR 3a) validates mandate cites at action time.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .mandate.types import (  # direct import: mandate/__init__.py is incomplete until backend.py lands in a sibling PR
    Mandate,
    MandateConstraints,
    MandateInvalid,
    ProjectMandateMeta,
    RevocationState,
    TargetPattern,
    TimeWindow,
)

logger = logging.getLogger(__name__)

# Mandate ID charset: lowercase letters, digits, hyphens; starts with
# alphanumeric. Derived from spec/29 §"Parser rules" line 299.
_MANDATE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_MANDATE_ID_MAX_LEN = 64

# Required top-level fields per spec/29 §"Parser rules" line 294.
_REQUIRED_FIELDS = frozenset({"granted_by", "granted_at", "scope", "revocation_state"})

# Structured enforcement fields. A mandate missing all of these (and without
# ``unconstrained: true``) is refused at load time per spec/29 §"Constraint
# enforceability" lines 277-286.
_ENFORCEMENT_FIELDS = frozenset(
    {
        "allowed_tools",
        "allowed_targets",
        "daily_token_usd",
        "monthly_token_usd",
        "cumulative_token_usd",
        "daily_external_usd",
        "monthly_external_usd",
        "cumulative_external_usd",
        "requires_escalation_above_token_usd",
        "requires_escalation_above_external_usd",
        "time_window",
    }
)

# Section splitter: match ``## <anything>`` at the start of a line.
_SECTION_HEADING_RE = re.compile(r"^## (.+)$", re.MULTILINE)

# Per-agent mandate policy values (spec/29 §"Resolution rules" line 439).
_VALID_PER_AGENT_POLICIES = frozenset({"open", "listed", "forbidden"})

# Accepted revocation_state values from the operator. ``expired`` is
# **derived** (computed from expires_at vs. current time) — operators
# must not write it directly (spec/29 §"Parser rules" line 297).
_VALID_REVOCATION_STATES = frozenset({"active", "revoked"})


# ──────────────────────────────────────────────────────────────────
# Public entry point


def parse_mandates_md(
    path: Path,
    *,
    scope: str,
    is_project_root: bool,
) -> tuple[ProjectMandateMeta | None, list[Mandate]]:
    """Parse a ``mandates.md`` file at ``path`` for the given scope.

    Args:
        path: Filesystem path to the ``mandates.md`` file.
        scope: Scope identifier string — either ``"agent:<name>"`` or
            ``"project:<name>"``. Written into every returned
            ``Mandate.scope`` field.
        is_project_root: When ``True``, a ``## _meta`` section is parsed
            and returned as the ``ProjectMandateMeta``. When ``False``,
            a ``## _meta`` section triggers a ``logging.warning`` and is
            silently skipped (doctor surfaces via
            ``check_mandate_meta_misplaced``).

    Returns:
        A tuple ``(meta, mandates)`` where:

        - ``meta`` is the parsed ``ProjectMandateMeta`` (``None`` if no
          ``_meta`` section was found, or if ``is_project_root=False``).
        - ``mandates`` is an ordered list of ``Mandate`` instances, one
          per non-``_meta`` section in the file.

    Raises:
        MandateInvalid: For any load-time validation failure. The
            exception message collects all errors found so the operator
            receives one alert, not a cascade of re-reads.

    Filesystem contract:
        - File missing → ``(None, [])``.
        - File empty or whitespace-only → ``(None, [])``.
        - Non-UTF-8 bytes → ``MandateInvalid``.
        - ``OSError`` reading the file → ``MandateInvalid``.
    """
    if not path.exists():
        return None, []

    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise MandateInvalid(
            f"could not read mandates.md at {path}: {exc}"
        ) from exc

    try:
        text = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise MandateInvalid(
            f"mandates.md at {path} is not valid UTF-8: {exc}"
        ) from exc

    if not text.strip():
        return None, []

    sections = _split_into_sections(text)
    if not sections:
        return None, []

    meta: ProjectMandateMeta | None = None
    mandates: list[Mandate] = []
    errors: list[str] = []
    seen_ids: dict[str, int] = {}  # id → first-seen section index

    for section_idx, (section_id, section_body) in enumerate(sections):
        # ── reserved _meta section ────────────────────────────────
        if section_id == "_meta":
            if not is_project_root:
                logger.warning(
                    "mandates.md at %s contains a ## _meta section but "
                    "is_project_root=False. The _meta section is silently "
                    "skipped. If this file is meant to be the project-root "
                    "mandates.md, move it to the project root. The doctor "
                    "check check_mandate_meta_misplaced will surface this "
                    "warning on the next doctor run.",
                    path,
                )
            else:
                try:
                    meta_yaml = _load_section_yaml(section_id, section_body)
                    meta = _parse_meta(meta_yaml)
                except MandateInvalid as exc:
                    errors.append(str(exc))
            continue

        # ── validate mandate ID ───────────────────────────────────
        try:
            _validate_mandate_id(section_id)
        except MandateInvalid as exc:
            errors.append(str(exc))
            continue

        # ── duplicate ID check ────────────────────────────────────
        if section_id in seen_ids:
            errors.append(
                f"mandate id {section_id!r} appears more than once in "
                f"{path}. Duplicate IDs are not allowed (spec/29 line 298). "
                f"First occurrence at section #{seen_ids[section_id] + 1}, "
                f"duplicate at section #{section_idx + 1}."
            )
            continue
        seen_ids[section_id] = section_idx

        # ── compute source_hash ───────────────────────────────────
        source_hash = _compute_source_hash(section_body)

        # ── parse the mandate ─────────────────────────────────────
        try:
            mandate = _parse_one_mandate(
                section_id=section_id,
                section_body=section_body,
                scope=scope,
                source_hash=source_hash,
                source_path=str(path),
            )
            mandates.append(mandate)
        except MandateInvalid as exc:
            errors.append(str(exc))

    if errors:
        bullet_list = "\n".join(f"  - {e}" for e in errors)
        raise MandateInvalid(
            f"mandates.md at {path} failed validation with "
            f"{len(errors)} error(s):\n{bullet_list}"
        )

    return meta, mandates


# ──────────────────────────────────────────────────────────────────
# Section splitting


def _split_into_sections(text: str) -> list[tuple[str, str]]:
    """Split markdown text into ``(section_id, section_body)`` pairs.

    Returns one tuple per ``## <heading>`` found. The section body is the
    raw text between the heading line and the next ``## `` heading (or
    EOF). Leading/trailing whitespace is stripped from each body.

    Headings with blank IDs (``## `` with no text) are skipped.
    Non-``## `` content before the first heading (e.g. a ``# Title``
    line) is silently ignored — matching how ``judges.md`` ignores
    preamble before the first YAML block.
    """
    matches = list(_SECTION_HEADING_RE.finditer(text))
    if not matches:
        return []

    sections: list[tuple[str, str]] = []
    for i, match in enumerate(matches):
        section_id = match.group(1).strip()
        if not section_id:
            continue  # skip blank headings
        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()
        sections.append((section_id, body))

    return sections


# ──────────────────────────────────────────────────────────────────
# Mandate ID validation


def _validate_mandate_id(mandate_id: str) -> None:
    """Raise ``MandateInvalid`` if ``mandate_id`` fails the spec/29 rules.

    Rules (spec/29 §"Parser rules" line 299):
    - Pattern: ``[a-z0-9][a-z0-9-]*``
    - Max length: 64 characters
    """
    if len(mandate_id) > _MANDATE_ID_MAX_LEN:
        raise MandateInvalid(
            f"mandate id {mandate_id!r} is {len(mandate_id)} characters long; "
            f"maximum allowed is {_MANDATE_ID_MAX_LEN} (spec/29 line 299)."
        )
    if not _MANDATE_ID_RE.match(mandate_id):
        raise MandateInvalid(
            f"mandate id {mandate_id!r} does not match the required pattern "
            f"[a-z0-9][a-z0-9-]* (lowercase letters, digits, hyphens; must "
            f"start with a letter or digit). Received: {mandate_id!r}"
        )


# ──────────────────────────────────────────────────────────────────
# Source hash


def _compute_source_hash(section_body: str) -> str:
    """Compute the SHA-256 hash of the canonical section bytes.

    Canonical form: ``section_body`` stripped of leading/trailing
    whitespace, with ``\\n`` line endings (CRLF normalized). The hash is
    returned as ``sha256:<hex>`` — algorithm-prefixed per the established
    project convention (mirrors ``binding.tool_definition_hash`` shape
    in spec/28). Future migration to a different algorithm changes the
    prefix; legacy readers see the prefix and fail loudly rather than
    silently mis-compare.
    """
    canonical = section_body.strip().replace("\r\n", "\n").replace("\r", "\n")
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ──────────────────────────────────────────────────────────────────
# YAML loading (shared)


def _load_section_yaml(section_id: str, section_body: str) -> dict[str, Any]:
    """Parse ``section_body`` as YAML and return the mapping.

    Raises ``MandateInvalid`` on YAML syntax errors or when the top-level
    value is not a mapping (the mandate format requires a flat YAML
    document per section). An empty body (no operator content) returns
    an empty ``dict`` so the required-field check can produce a useful
    error message.

    Never calls ``yaml.load`` — alias-bomb DoS risk (spec/25 PR 1
    lesson). Always uses ``yaml.safe_load``.
    """
    if not section_body.strip():
        return {}
    try:
        obj = yaml.safe_load(section_body)
    except yaml.YAMLError as exc:
        raise MandateInvalid(
            f"mandate section {section_id!r}: invalid YAML: {exc}"
        ) from exc
    if obj is None:
        return {}
    if not isinstance(obj, dict):
        raise MandateInvalid(
            f"mandate section {section_id!r}: YAML must be a mapping "
            f"(got {type(obj).__name__}). Each mandate section body is a "
            f"flat YAML document with fields like granted_by, granted_at, etc."
        )
    return obj


# ──────────────────────────────────────────────────────────────────
# Single-mandate parser


def _parse_one_mandate(
    section_id: str,
    section_body: str,
    scope: str,
    source_hash: str,
    source_path: str | None,
) -> Mandate:
    """Parse one mandate section body into a ``Mandate`` dataclass.

    Args:
        section_id: The mandate ID (heading text, already validated).
        section_body: Raw YAML text (already stripped) for this section.
        scope: Propagated from ``parse_mandates_md``'s ``scope`` arg.
        source_hash: Pre-computed SHA-256 of the canonical section bytes.
        source_path: Filesystem path string for ``Mandate.source_path``.

    Raises:
        MandateInvalid: On any field-level validation failure. Named by
            ``section_id`` so the error is actionable from the aggregated
            caller.
    """
    raw = _load_section_yaml(section_id, section_body)

    # ── required fields ───────────────────────────────────────────
    missing = _REQUIRED_FIELDS - set(raw.keys())
    if missing:
        raise MandateInvalid(
            f"mandate {section_id!r}: missing required field(s): "
            f"{sorted(missing)}. Required: {sorted(_REQUIRED_FIELDS)}"
        )

    granted_by = _coerce_str(raw["granted_by"], field=f"{section_id}.granted_by")
    granted_at = _parse_datetime(raw["granted_at"], field=f"{section_id}.granted_at")
    expires_at_raw = raw.get("expires_at")
    expires_at = (
        _parse_datetime(expires_at_raw, field=f"{section_id}.expires_at")
        if expires_at_raw not in (None, "null", "")
        else None
    )

    revocable_by = _coerce_str(
        raw.get("revocable_by", "operator"), field=f"{section_id}.revocable_by"
    )

    # ``scope`` in the YAML is the prose description of authority, distinct
    # from the parser's ``scope`` arg (which is the agent/project scope label).
    prose_scope = _coerce_str(raw["scope"], field=f"{section_id}.scope")  # noqa: F841

    revocation_state = _parse_revocation_state(
        raw["revocation_state"], section_id=section_id
    )

    revoked_at_raw = raw.get("revoked_at")
    revoked_at = (
        _parse_datetime(revoked_at_raw, field=f"{section_id}.revoked_at")
        if revoked_at_raw not in (None, "null", "")
        else None
    )

    revoked_by_raw = raw.get("revoked_by")
    revoked_by = (
        str(revoked_by_raw)
        if revoked_by_raw not in (None, "null", "")
        else None
    )

    revocation_reason_raw = raw.get("revocation_reason")
    revocation_reason = (
        str(revocation_reason_raw)
        if revocation_reason_raw not in (None, "null", "")
        else None
    )

    # ── constraints ───────────────────────────────────────────────
    constraints_raw = raw.get("constraints")
    constraints = _parse_constraints(
        constraints_raw if isinstance(constraints_raw, dict) else {},
        section_id=section_id,
    )

    # ── constraint enforceability check ──────────────────────────
    _check_enforceability(constraints, section_id=section_id)

    return Mandate(
        mandate_id=section_id,
        scope=scope,
        granted_by=granted_by,
        granted_at=granted_at,
        expires_at=expires_at,
        revocation_state=revocation_state,
        revoked_at=revoked_at,
        revoked_by=revoked_by,
        revocation_reason=revocation_reason,
        constraints=constraints,
        source_hash=source_hash,
        source_path=source_path,
    )


# ──────────────────────────────────────────────────────────────────
# Constraint parser


def _parse_constraints(raw: dict[str, Any], *, section_id: str) -> MandateConstraints:
    """Convert a raw YAML ``constraints:`` dict to ``MandateConstraints``.

    All fields default to the ``MandateConstraints`` dataclass defaults
    when absent from the operator's YAML.
    """
    allowed_tools_raw = raw.get("allowed_tools")
    allowed_tools: frozenset[str] = frozenset()
    if allowed_tools_raw is not None:
        if not isinstance(allowed_tools_raw, list):
            raise MandateInvalid(
                f"mandate {section_id!r}: constraints.allowed_tools must be "
                f"a list of tool name strings; got {type(allowed_tools_raw).__name__}"
            )
        for item in allowed_tools_raw:
            if not isinstance(item, str):
                raise MandateInvalid(
                    f"mandate {section_id!r}: constraints.allowed_tools item "
                    f"{item!r} must be a string; got {type(item).__name__}"
                )
        allowed_tools = frozenset(allowed_tools_raw)

    allowed_targets = _parse_target_list(
        raw.get("allowed_targets"), field=f"{section_id}.constraints.allowed_targets"
    )
    blocked_targets = _parse_target_list(
        raw.get("blocked_targets"), field=f"{section_id}.constraints.blocked_targets"
    )

    time_window_raw = raw.get("time_window")
    time_window = (
        _parse_time_window(time_window_raw, section_id=section_id)
        if time_window_raw is not None
        else None
    )

    daily_token_usd = _coerce_usd(
        raw.get("daily_token_usd"), field=f"{section_id}.constraints.daily_token_usd"
    )
    monthly_token_usd = _coerce_usd(
        raw.get("monthly_token_usd"), field=f"{section_id}.constraints.monthly_token_usd"
    )
    cumulative_token_usd = _coerce_usd(
        raw.get("cumulative_token_usd"),
        field=f"{section_id}.constraints.cumulative_token_usd",
    )

    daily_external_usd = _coerce_usd(
        raw.get("daily_external_usd"),
        field=f"{section_id}.constraints.daily_external_usd",
    )
    monthly_external_usd = _coerce_usd(
        raw.get("monthly_external_usd"),
        field=f"{section_id}.constraints.monthly_external_usd",
    )
    cumulative_external_usd = _coerce_usd(
        raw.get("cumulative_external_usd"),
        field=f"{section_id}.constraints.cumulative_external_usd",
    )

    requires_escalation_above_token_usd = _coerce_usd(
        raw.get("requires_escalation_above_token_usd"),
        field=f"{section_id}.constraints.requires_escalation_above_token_usd",
    )
    requires_escalation_above_external_usd = _coerce_usd(
        raw.get("requires_escalation_above_external_usd"),
        field=f"{section_id}.constraints.requires_escalation_above_external_usd",
    )

    unconstrained_raw = raw.get("unconstrained", False)
    if not isinstance(unconstrained_raw, bool):
        raise MandateInvalid(
            f"mandate {section_id!r}: constraints.unconstrained must be "
            f"a boolean (true/false); got {type(unconstrained_raw).__name__}={unconstrained_raw!r}"
        )
    unconstrained = unconstrained_raw

    unconstrained_justification_raw = raw.get("unconstrained_justification")
    unconstrained_justification = (
        str(unconstrained_justification_raw)
        if unconstrained_justification_raw not in (None, "")
        else None
    )

    return MandateConstraints(
        allowed_tools=allowed_tools,
        allowed_targets=tuple(allowed_targets),
        blocked_targets=tuple(blocked_targets),
        time_window=time_window,
        daily_token_usd=daily_token_usd,
        monthly_token_usd=monthly_token_usd,
        cumulative_token_usd=cumulative_token_usd,
        daily_external_usd=daily_external_usd,
        monthly_external_usd=monthly_external_usd,
        cumulative_external_usd=cumulative_external_usd,
        requires_escalation_above_token_usd=requires_escalation_above_token_usd,
        requires_escalation_above_external_usd=requires_escalation_above_external_usd,
        unconstrained=unconstrained,
        unconstrained_justification=unconstrained_justification,
    )


def _check_enforceability(constraints: MandateConstraints, *, section_id: str) -> None:
    """Raise ``MandateInvalid`` when the mandate lacks structured enforcement.

    Per spec/29 §"Constraint enforceability" lines 277-286: a mandate is
    refused at load time unless at least one structured enforcement field
    is non-empty. The sole opt-out is ``unconstrained: true`` with a
    non-empty ``unconstrained_justification``.
    """
    if constraints.unconstrained:
        if not constraints.unconstrained_justification:
            raise MandateInvalid(
                f"mandate {section_id!r}: constraints.unconstrained is True "
                f"but unconstrained_justification is missing or empty. "
                f"Operators opting out of structured enforcement must supply "
                f"a non-empty justification string (spec/29 lines 283-286). "
                f"Example: unconstrained_justification: \"Trust-the-prose; "
                f"manually reviewed on each run.\""
            )
        return  # unconstrained + justification → valid

    has_enforcement = (
        bool(constraints.allowed_tools)
        or bool(constraints.allowed_targets)
        or constraints.daily_token_usd is not None
        or constraints.monthly_token_usd is not None
        or constraints.cumulative_token_usd is not None
        or constraints.daily_external_usd is not None
        or constraints.monthly_external_usd is not None
        or constraints.cumulative_external_usd is not None
        or constraints.requires_escalation_above_token_usd is not None
        or constraints.requires_escalation_above_external_usd is not None
        or constraints.time_window is not None
    )

    if not has_enforcement:
        raise MandateInvalid(
            f"mandate {section_id!r}: no structured enforcement constraints "
            f"are declared. The framework enforces constraints, not prose — "
            f"a mandate without structured constraints provides no runtime "
            f"enforcement (spec/29 §'Constraint enforceability'). Add at "
            f"least one of: allowed_tools, allowed_targets, a *_token_usd "
            f"cap, a *_external_usd cap, requires_escalation_above_*, or "
            f"time_window. Alternatively, set constraints.unconstrained: true "
            f"with a non-empty unconstrained_justification to acknowledge the "
            f"scope-only authorization explicitly."
        )


# ──────────────────────────────────────────────────────────────────
# Target list parser


def _parse_target_list(
    raw: Any, *, field: str
) -> list[TargetPattern]:
    """Parse an ``allowed_targets`` or ``blocked_targets`` list.

    Each element is either:
    - A bare string → ``TargetPattern(pattern=str, kind="exact")``
    - A dict ``{kind: <kind_str>, value: <pattern_str>}`` → uses the
      explicit kind + value keys from the canonical format in spec/29
      §"The Mandate dataclass" lines 156-163.
    - A dict ``{prefix: <pattern_str>}`` → shorthand convenience form
      for ``kind="prefix"`` (per the spec/29 §"mandates.md file format"
      lines 244-250 example).

    Returns an empty list when ``raw`` is ``None``.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise MandateInvalid(
            f"{field} must be a list; got {type(raw).__name__}"
        )
    result: list[TargetPattern] = []
    for idx, item in enumerate(raw):
        if isinstance(item, str):
            result.append(TargetPattern(pattern=item, kind="exact"))
        elif isinstance(item, dict):
            # Shorthand: {prefix: "foo.*"}
            if "prefix" in item and "kind" not in item and "value" not in item:
                prefix_val = item["prefix"]
                if not isinstance(prefix_val, str):
                    raise MandateInvalid(
                        f"{field}[{idx}]: prefix value must be a string; "
                        f"got {type(prefix_val).__name__}={prefix_val!r}"
                    )
                result.append(TargetPattern(pattern=prefix_val, kind="prefix"))
            # Canonical: {kind: "...", value: "..."}
            elif "kind" in item and "value" in item:
                kind_val = item["kind"]
                value_val = item["value"]
                if not isinstance(kind_val, str):
                    raise MandateInvalid(
                        f"{field}[{idx}]: kind must be a string; "
                        f"got {type(kind_val).__name__}={kind_val!r}"
                    )
                if not isinstance(value_val, str):
                    raise MandateInvalid(
                        f"{field}[{idx}]: value must be a string; "
                        f"got {type(value_val).__name__}={value_val!r}"
                    )
                # Map canonical kind names from spec/28's TargetPattern.kind
                # ("vendor", "url_glob", etc.) to the types.py "exact"/"prefix"
                # taxonomy. Unrecognized kind values are preserved as-is —
                # the parser records them; MandateCheck handles matching.
                kind_normalized = kind_val.lower().strip()
                result.append(TargetPattern(pattern=value_val, kind=kind_normalized))
            else:
                raise MandateInvalid(
                    f"{field}[{idx}]: target dict must be either "
                    f"{{prefix: <pattern>}} or {{kind: <kind>, value: "
                    f"<pattern>}}; got keys {sorted(item.keys())!r}"
                )
        else:
            raise MandateInvalid(
                f"{field}[{idx}]: target entry must be a string or a "
                f"mapping; got {type(item).__name__}={item!r}"
            )
    return result


# ──────────────────────────────────────────────────────────────────
# Time window parser


def _parse_time_window(raw: Any, *, section_id: str) -> TimeWindow:
    """Parse a ``time_window:`` dict into a ``TimeWindow`` dataclass.

    Accepted shape (per spec/29 §"The Mandate dataclass" lines 163-168):

    .. code-block:: yaml

        time_window:
          start: "09:00"
          end: "17:00"

    Both ``start`` and ``end`` must be present in ``HH:MM`` format.
    Parses into UTC ``datetime.time`` instances (the framework treats
    all time_window values as UTC per the ``TimeWindow`` docstring).

    Raises ``MandateInvalid`` when:
    - ``raw`` is not a mapping.
    - ``start`` or ``end`` is missing.
    - Time strings cannot be parsed.
    - ``start_utc == end_utc`` (ambiguous; spec/29 §TimeWindow).
    """
    if not isinstance(raw, dict):
        raise MandateInvalid(
            f"mandate {section_id!r}: constraints.time_window must be a "
            f"mapping with 'start' and 'end' keys; got {type(raw).__name__}"
        )
    start_raw = raw.get("start")
    end_raw = raw.get("end")
    if start_raw is None or end_raw is None:
        raise MandateInvalid(
            f"mandate {section_id!r}: constraints.time_window requires "
            f"both 'start' and 'end' keys (e.g. start: '09:00', end: '17:00')"
        )
    start_utc = _parse_time_of_day(str(start_raw), field=f"{section_id}.time_window.start")
    end_utc = _parse_time_of_day(str(end_raw), field=f"{section_id}.time_window.end")
    if start_utc == end_utc:
        raise MandateInvalid(
            f"mandate {section_id!r}: constraints.time_window start and end "
            f"are identical ({start_utc!s}). An equal start/end is ambiguous "
            f"(zero-length or 24-hour window?). Use distinct times or omit "
            f"time_window to apply no time restriction."
        )
    return TimeWindow(start_utc=start_utc, end_utc=end_utc)


def _parse_time_of_day(raw: str, *, field: str):  # -> datetime.time
    """Parse an ``HH:MM`` or ``HH:MM:SS`` string as a ``datetime.time``.

    Returns a ``datetime.time`` instance (imported as ``dt_time`` in
    types.py). Raises ``MandateInvalid`` on any format error.
    """
    from datetime import time as dt_time  # avoid shadowing at module level

    try:
        parts = raw.strip().split(":")
        if len(parts) == 2:
            return dt_time(int(parts[0]), int(parts[1]))
        elif len(parts) == 3:
            return dt_time(int(parts[0]), int(parts[1]), int(parts[2]))
        else:
            raise ValueError(f"unexpected format: {raw!r}")
    except (ValueError, TypeError) as exc:
        raise MandateInvalid(
            f"{field}: could not parse time-of-day {raw!r}. "
            f"Expected HH:MM or HH:MM:SS (24-hour UTC). Detail: {exc}"
        ) from exc


# ──────────────────────────────────────────────────────────────────
# _meta parser


def _parse_meta(raw: dict[str, Any]) -> ProjectMandateMeta:
    """Parse a ``## _meta`` section body into a ``ProjectMandateMeta``.

    Expected YAML shape:

    .. code-block:: yaml

        per_agent_mandate_policy: open   # or "listed" or "forbidden"
        allowed_per_agent_ids:
          - test-mandate-A
          - test-mandate-B

    Per spec/29 §"Resolution rules" line 439:
    - ``per_agent_mandate_policy`` defaults to ``"open"`` when absent.
    - ``allowed_per_agent_ids`` is only meaningful when the policy is
      ``"listed"``; it is stored (and validated) for any policy value
      to allow operator-authored files to declare the list ahead of a
      policy switch.

    Raises ``MandateInvalid`` on unknown policy values or malformed IDs.
    """
    policy_raw = raw.get("per_agent_mandate_policy", "open")
    if not isinstance(policy_raw, str):
        raise MandateInvalid(
            f"_meta.per_agent_mandate_policy must be a string "
            f"({sorted(_VALID_PER_AGENT_POLICIES)}); "
            f"got {type(policy_raw).__name__}={policy_raw!r}"
        )
    policy = policy_raw.lower().strip()
    if policy not in _VALID_PER_AGENT_POLICIES:
        raise MandateInvalid(
            f"_meta.per_agent_mandate_policy {policy_raw!r} is not valid. "
            f"Allowed values: {sorted(_VALID_PER_AGENT_POLICIES)}"
        )

    ids_raw = raw.get("allowed_per_agent_ids")
    allowed_ids: frozenset[str] = frozenset()
    if ids_raw is not None:
        if not isinstance(ids_raw, list):
            raise MandateInvalid(
                f"_meta.allowed_per_agent_ids must be a list of mandate ID "
                f"strings; got {type(ids_raw).__name__}"
            )
        validated_ids: list[str] = []
        for item in ids_raw:
            if not isinstance(item, str):
                raise MandateInvalid(
                    f"_meta.allowed_per_agent_ids entry {item!r} must be a "
                    f"string; got {type(item).__name__}"
                )
            _validate_mandate_id(item)
            validated_ids.append(item)
        allowed_ids = frozenset(validated_ids)

    return ProjectMandateMeta(
        per_agent_mandate_policy=policy,  # type: ignore[arg-type]
        allowed_per_agent_ids=allowed_ids,
    )


# ──────────────────────────────────────────────────────────────────
# Field coercion helpers


def _coerce_str(raw: Any, *, field: str) -> str:
    """Coerce a scalar YAML value to ``str``. Raises ``MandateInvalid``
    when the value is ``None`` or not representable as a non-empty string.
    """
    if raw is None:
        raise MandateInvalid(
            f"{field}: required string field is null/missing"
        )
    value = str(raw).strip()
    if not value:
        raise MandateInvalid(
            f"{field}: required string field is empty"
        )
    return value


def _coerce_usd(raw: Any, *, field: str) -> float | None:
    """Coerce a YAML value to a non-negative float USD amount, or ``None``.

    Returns ``None`` when ``raw`` is ``None``. Raises ``MandateInvalid``
    on booleans (bool is an int subclass — reject explicitly), negative
    values, or non-numeric types.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise MandateInvalid(
            f"{field}: USD amount must be a number; got boolean {raw!r}"
        )
    if not isinstance(raw, (int, float)):
        raise MandateInvalid(
            f"{field}: USD amount must be a number; got {type(raw).__name__}={raw!r}"
        )
    value = float(raw)
    if value < 0:
        raise MandateInvalid(
            f"{field}: USD amount must be >= 0; got {value}"
        )
    return value


def _parse_datetime(raw: Any, *, field: str) -> datetime:
    """Parse an ISO-8601 datetime string into a timezone-aware ``datetime``.

    Handles the common operator formats:
    - ``2026-04-01T09:00:00Z`` → UTC-aware datetime.
    - ``2026-04-01T09:00:00+05:00`` → offset-aware datetime.
    - ``2026-04-01`` → date-only, interpreted as midnight UTC.
    - YAML may parse date strings as ``datetime.date`` objects — we
      coerce them back to strings first.

    Raises ``MandateInvalid`` on unrecognised formats, forwarding the
    parse error detail for operator actionability.
    """
    from datetime import date as dt_date

    if isinstance(raw, dt_date) and not isinstance(raw, datetime):
        # YAML parsed a bare date (e.g. ``2026-04-01``). Treat as midnight UTC.
        return datetime(raw.year, raw.month, raw.day, tzinfo=timezone.utc)

    if isinstance(raw, datetime):
        # YAML parsed a full datetime. Ensure it is timezone-aware.
        if raw.tzinfo is None:
            return raw.replace(tzinfo=timezone.utc)
        return raw

    if raw is None:
        raise MandateInvalid(f"{field}: datetime field is null/missing")

    raw_str = str(raw).strip()
    if not raw_str:
        raise MandateInvalid(f"{field}: datetime field is empty")

    # Normalise trailing 'Z' → '+00:00' for Python < 3.11 fromisoformat.
    normalised = raw_str
    if normalised.endswith("Z"):
        normalised = normalised[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(normalised)
    except ValueError as exc:
        raise MandateInvalid(
            f"{field}: could not parse datetime {raw_str!r}. "
            f"Expected ISO-8601 format (e.g. '2026-04-01T09:00:00Z' or "
            f"'2026-04-01'). Detail: {exc}"
        ) from exc

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt


def _parse_revocation_state(raw: Any, *, section_id: str) -> RevocationState:
    """Parse the ``revocation_state`` field.

    Accepted values: ``active`` | ``revoked``. The value ``expired`` is
    **derived** state (computed at load time from ``expires_at`` vs.
    current time); operators must not write it directly (spec/29 line 297).

    Raises ``MandateInvalid`` on any other value, including ``expired``
    (with a targeted message explaining it is derived, not authored).
    """
    if not isinstance(raw, str):
        raise MandateInvalid(
            f"mandate {section_id!r}: revocation_state must be a string "
            f"({sorted(_VALID_REVOCATION_STATES)}); got "
            f"{type(raw).__name__}={raw!r}"
        )
    normalised = raw.lower().strip()
    if normalised == "expired":
        raise MandateInvalid(
            f"mandate {section_id!r}: revocation_state: expired is not a "
            f"valid operator-authored value. 'expired' is a derived state "
            f"the framework computes from expires_at vs. the current time. "
            f"To mark a mandate inactive, set revocation_state: revoked "
            f"(spec/29 line 297)."
        )
    if normalised not in _VALID_REVOCATION_STATES:
        raise MandateInvalid(
            f"mandate {section_id!r}: revocation_state {raw!r} is not valid. "
            f"Allowed: {sorted(_VALID_REVOCATION_STATES)}"
        )
    return RevocationState(normalised)

"""``manage govern <agent>`` verb — governance.md frontmatter editor (spec/55 #609).

Implements the S2 five-step safety routine for the ``govern`` verb:
  1. Validate   — field names + enum values; PRESENT_INVALID guard; dotted-path guard
  2. Preview    — before/after diff of changed governance fields (incl. auto-stamp)
  3. Confirm    — --dry-run exits; --yes or TTY prompt
  4. Snapshot + Write — snapshot prior content; atomic_write new content (M2/M3)
  5. Audit      — RunRecord appended to per-agent + fleet LogBackend (M8)

M2 surgical preservation contract:
  - Prose body outside the ```yaml ... ``` governance block is preserved byte-for-byte.
  - Untargeted keys inside the block — incl. inline comments and key order — survive
    byte-for-byte.
  - Only the targeted scalar value is rewritten in place — EXCEPT the ``updated_at``
    auto-stamp (named exception to (c), spec/55 M2): every applied write also stamps
    ``updated_at`` to today's date unless the operator set it explicitly, so a
    non-targeted key is rewritten (or inserted) on essentially every run. It is a
    genuine write (shown in the preview and the audit before→after), documented so
    it is never a silent surprise.
  - PyYAML is reader/validator only; safe_dump is NEVER used as the writer.

Governance field allowlist (PR1 — flat scalars only):
  owner, backup_owner, permission_tier, customer_data, writes_sor,
  lifecycle_status, created_at, updated_at

Nested fields (review.*, risk.*, sources.*, actions.*) are reserved for later PRs
and return a clean "edit governance.md directly" refusal (never a parser error).
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ..core_api import safe_resolve_under
from ..agent_registry.types import (
    PERMISSION_TIERS,
    TRISTATES,
    LIFECYCLE_STATUSES,
)
from ..logs.types import PRIMITIVE_MANAGE_GOVERN, PRIMITIVE_MANAGE_RESTORE, RunRecord
from ._routine import (
    ManagedWriteResult,
    append_management_audit,
    get_manage_lock_backend,
    list_snapshots,
    resolve_snapshot_path,
    run_managed_write,
)
from .exceptions import (
    ManageAgentBusyError,
    ManageControlCharRefused,
    ManageGovernanceInvalidError,
    ManageInvalidDateError,
    ManageInvalidEnumError,
    ManageListMutationRefused,
    ManageLockUnavailableError,
    ManageNestedPathRefused,
    ManageSnapshotNotFoundError,
    ManageUnknownFieldError,
)

# spec/55 #709 parameterization — govern's own snapshot namespace. Explicit,
# not the old module-global default: a future verb (set-model, apply-rec)
# names its own subdir so snapshot namespaces never collide (_routine.py's
# take_config_snapshot/list_snapshots/resolve_snapshot_path all take an
# explicit `subdir` argument now).
_SNAPSHOT_SUBDIR = "govern"


# ── Field metadata ─────────────────────────────────────────────────────────────

# PR1 flat-scalar allowlist: ALL top-level scalar fields on GovernanceRecord.
# (Nested sub-records review/risk/sources/actions are PR2+.)
_FLAT_SCALAR_FIELDS: frozenset[str] = frozenset(
    {
        "owner",
        "backup_owner",
        "permission_tier",
        "customer_data",
        "writes_sor",
        "lifecycle_status",
        "created_at",
        "updated_at",
    }
)

# Enum-validated fields: map schema key -> allowed-value frozenset.
_ENUM_FIELDS: dict[str, frozenset] = {
    "permission_tier": PERMISSION_TIERS,
    "customer_data": TRISTATES,
    "writes_sor": TRISTATES,
    "lifecycle_status": LIFECYCLE_STATUSES,
}

# Nested sub-record names (reserved, PR2+). Dotted paths or these names preceded
# by a dot trigger the "not yet settable via CLI" refusal.
_NESTED_FIELDS: frozenset[str] = frozenset({"review", "risk", "sources", "actions"})

# CLI hyphen form -> schema underscore form mapping.
_CLI_TO_SCHEMA: dict[str, str] = {f.replace("_", "-"): f for f in _FLAT_SCALAR_FIELDS}
# Also accept underscore form directly (idempotent mapping).
_CLI_TO_SCHEMA.update({f: f for f in _FLAT_SCALAR_FIELDS})

# Secret-shaped field names (none in governance, but applying the project's redact
# rule defensively — values that look like keys/tokens are redacted at echo sites).
_SECRET_SUBSTRINGS: tuple[str, ...] = (
    "key",
    "token",
    "secret",
    "password",
    "credential",
)

# Max string length for before/after values in audit extra{} (matches summary cap).
_AUDIT_VALUE_MAX_LEN = 200

# YAML governance block root key (matches filesystem.py _GOVERNANCE_KEY).
_GOVERNANCE_KEY = "governance"

# A conservatively-safe PLAIN scalar: starts alphanumeric, then alphanumerics /
# underscore / hyphen only. Enum values (writes, read-only, active) match and stay
# bare; anything with a space, ``@``, ``.``, ``:``, flow indicator, quote, backslash,
# ``#``, or non-ASCII does NOT match and is single-quote-escaped instead. This is a
# STRUCTURAL gate only — it excludes characters that could never be a bare plain
# scalar; type-coercion spellings (numbers, bools, dates) that survive this gate are
# caught authoritatively by the PyYAML round-trip in ``_needs_quoting``.
_PLAIN_SAFE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


# ── Value normalisation helpers ────────────────────────────────────────────────


def _today_iso() -> str:
    """Return today's date as an ISO-8601 string (YYYY-MM-DD)."""
    return date.today().isoformat()


def _is_null_value(raw: str) -> bool:
    """Return True when the CLI value is a null-clearing gesture."""
    return raw.lower() in ("null", "none", "")


def _is_secret_shaped(field: str) -> bool:
    """Return True when the FIELD NAME is secret-shaped.

    Detection is by field NAME only (the project's redact-at-echo rule keys on
    the schema key, per spec/55 M8), not by inspecting the value. No
    GovernanceRecord field name matches ``_SECRET_SUBSTRINGS`` today, so this is
    inert for the ``govern`` verb — but the redaction path is wired at every
    echo site so later verbs that DO carry a secret-shaped field inherit the
    correct shape.
    """
    field_lower = field.lower()
    return any(s in field_lower for s in _SECRET_SUBSTRINGS)


def _redact_if_secret(field: str, value: Any) -> Any:
    """Return the value unchanged, or ``<redacted>`` when the field is secret-shaped.

    Detection is by field NAME (see ``_is_secret_shaped``). Non-secret values pass
    through with their original type preserved (so the audit record keeps typed
    values); secret-shaped fields collapse to the ``<redacted>`` sentinel string.
    """
    if _is_secret_shaped(field):
        return "<redacted>"
    return value


def _cap_for_audit(value: Any) -> Any:
    """Cap a string value for storage in audit extra{} (spec/55 M8)."""
    if isinstance(value, str) and len(value) > _AUDIT_VALUE_MAX_LEN:
        return value[:_AUDIT_VALUE_MAX_LEN] + "..."
    return value


def _emit_yaml_scalar(new_value: str | None) -> str:
    """Serialise ``new_value`` as a valid YAML scalar for in-block emission (M2).

    None -> literal ``null``. Otherwise the value is emitted BARE when it is a
    conservatively-safe plain scalar (enum values, hyphenated tokens) and
    SINGLE-QUOTE-escaped otherwise. Single-quoted YAML does NOT process backslash
    escapes and safely carries double-quotes, brackets, ``#``, ``@``, ``:``, and
    non-ASCII; the only in-scalar special character is ``'``, escaped by doubling.
    The quote decision is made authoritatively against PyYAML's own implicit
    resolver (``_needs_quoting``), so the emitted scalar round-trips back to the
    identical Python ``str`` — unlike a naive ``f'"{v}"'`` wrap, which corrupts any
    value containing a double-quote or backslash and mis-types bare ``[a,b]`` /
    numbers on re-read.
    """
    if new_value is None:
        return "null"
    if _needs_quoting(new_value):
        return "'" + new_value.replace("'", "''") + "'"
    return new_value


def _needs_quoting(value: str) -> bool:
    """Return True when ``value`` cannot be emitted as a bare YAML plain scalar.

    Two gates:
      1. STRUCTURAL — an empty string or any value containing a character outside
         the conservative plain-safe set (space, ``@``, ``.``, ``:``, ``#``, quote,
         flow indicator, non-ASCII) can never be a bare plain scalar. Quote.
      2. TYPE-COERCION (authoritative) — a value that IS plain-safe but that
         PyYAML's implicit resolver would coerce to a non-``str`` (bool, null,
         decimal/hex/binary/underscore-grouped int, float, ISO date, sexagesimal,
         timestamp) must be quoted so it round-trips as the original string. Rather
         than hand-roll YAML 1.1's number/bool grammar (which misses ``0x10`` /
         ``0b101`` / ``12_000`` / ``on`` / ``off`` …), decide against PyYAML itself:
         quote unless the bare token ``safe_load``s back to the identical string.
    """
    if value == "":
        return True
    if _PLAIN_SAFE_RE.match(value) is None:
        return True
    try:
        loaded = yaml.safe_load(value)
    except Exception:  # noqa: BLE001
        # Any load failure means the bare token is not a losslessly-round-tripping
        # plain scalar, so it must be quoted. Catch broadly (not just YAMLError):
        # PyYAML's implicit timestamp resolver raises a plain ValueError for an
        # out-of-range date like ``2026-13-99`` (month > 12) from the constructor,
        # not a YAMLError — a narrow except leaks that up through _emit_yaml_scalar.
        return True
    return not (isinstance(loaded, str) and loaded == value)


# ── Field validation ───────────────────────────────────────────────────────────


def _parse_set_token(token: str) -> tuple[str, str | None]:
    """Parse a ``--set field=value`` token.

    Splits on the FIRST ``=`` only (values may contain ``=``, e.g. emails,
    base64, URLs). Returns (schema_field_key, value_or_none).
    ``value_or_none`` is None when the operator intends to clear the field
    (token is ``field=null`` / ``field=none`` / ``field=``).

    Raises:
        ValueError: if no ``=`` is present in the token.
        ManageUnknownFieldError: if the field is unrecognised.
        ManageNestedPathRefused: if the field is a dotted path (PR2+).
    """
    field_raw, sep, value_raw = token.partition("=")
    if not sep:
        raise ValueError(
            f"Invalid --set syntax: expected field=value, got {token!r}. "
            "Example: --set owner=alice@example.com"
        )

    field_raw = field_raw.strip()

    # Dotted-path check first (must precede the known-field check so the error
    # message is correct: "nested field — edit directly" not "unknown field").
    if "." in field_raw:
        raise ManageNestedPathRefused(field_raw)

    # Map CLI hyphen form to schema underscore form.
    schema_key = _CLI_TO_SCHEMA.get(field_raw)
    if schema_key is None:
        # Check if it is a nested sub-field name used without a dot.
        if field_raw.replace("-", "_") in _NESTED_FIELDS:
            raise ManageNestedPathRefused(field_raw)
        raise ManageUnknownFieldError(field_raw)

    value: str | None = value_raw
    if _is_null_value(value_raw):
        value = None

    return schema_key, value


_DATE_FIELDS: frozenset[str] = frozenset({"created_at", "updated_at"})


def _is_iso_date(value: str) -> bool:
    """Return True when ``value`` is a well-formed ISO-8601 ``YYYY-MM-DD`` date."""
    if _ISO_DATE_RE.match(value) is None:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _validate_field_value(schema_key: str, value: str | None) -> None:
    """Validate a parsed field value against enum + date-format constraints (M4).

    None values (clearing) skip validation.

    Raises:
        ManageControlCharRefused: if value carries a newline / control character.
        ManageInvalidEnumError: if value is not in the allowed enum set.
        ManageInvalidDateError: if a date field is not a valid ISO-8601 date.
    """
    if value is None:
        return  # Clearing a field is always valid.

    # Reject any value that cannot be emitted to YAML and read back byte-identical.
    # A single-quoted YAML scalar folds an embedded line break into a space on
    # re-read, so a value carrying a newline, carriage return, or U+0085 NEL (which
    # PyYAML also treats as a line break) would persist differently from what
    # --json reports — making the durable audit record lie about what is on disk
    # (principle #5). Rather than hand-roll the C0/DEL/NEL/line-separator grammar
    # (a partial blocklist misses the next folding/normalising code point), decide
    # against PyYAML itself: emit the scalar the writer would emit and refuse
    # unless it round-trips to the identical string — the same authoritative idiom
    # ``_needs_quoting`` uses. Governance scalars are single-line, so this is a
    # clean refusal rather than a silent value-mangling write. Applies to every
    # field (enums never contain controls; the free-text owner / backup_owner
    # fields are the reachable case).
    emitted = _emit_yaml_scalar(value)
    try:
        reloaded: Any = yaml.safe_load(emitted)
    except yaml.YAMLError:
        # PyYAML rejects some control chars (NUL, vertical tab) outright — a value
        # it will not read back at all cannot persist losslessly, so refuse.
        reloaded = object()  # sentinel that is never equal to a str value
    if reloaded != value:
        raise ManageControlCharRefused(schema_key)

    # Date fields (created_at / updated_at) MUST be ISO-8601 YYYY-MM-DD.
    if schema_key in _DATE_FIELDS and not _is_iso_date(value):
        raise ManageInvalidDateError(schema_key, value)

    allowed = _ENUM_FIELDS.get(schema_key)
    if allowed is not None and value not in allowed:
        # Tristate ``true``/``false`` interop is handled UPSTREAM by
        # ``_normalise_tristate`` (called before this validator in run_govern), so
        # by the time an enum value reaches here it is already the canonical
        # yes/no spelling. Any value still outside the allowed set is a real error.
        raise ManageInvalidEnumError(schema_key, value, allowed)


def _normalise_tristate(value: str | None) -> str | None:
    """Normalise CLI tristate spellings (true/false -> yes/no)."""
    if value is None:
        return None
    low = value.lower()
    if low == "true":
        return "yes"
    if low == "false":
        return "no"
    return value


# ── Surgical YAML block editor ────────────────────────────────────────────────


def _split_scalar_and_comment(rest: str) -> tuple[str, str]:
    """Split the post-``key:`` region of a line into (value, comment_suffix).

    ``rest`` is everything after the ``key:`` separator (it may carry leading
    whitespace). Returns:
      - ``value``: the raw scalar text WITH its original leading whitespace, and
        with trailing whitespace peeled into ``comment_suffix``. Empty string when
        the line has no value (only a comment, or nothing).
      - ``comment_suffix``: a genuine trailing comment INCLUDING its leading
        whitespace, or just the trailing-whitespace run when there is no comment.
        Preserved verbatim so the targeted line's comment survives the edit (M2).

    A ``#`` starts a YAML comment ONLY when it is preceded by whitespace AND is
    not inside a quoted scalar. So ``alice#1`` is the single scalar ``alice#1``
    (no comment), ``'a # b'`` is a quoted scalar (its inner `` # `` is not a
    comment), and ``alice # note`` splits into value ``alice`` / comment
    `` # note``. This is what makes the value-replace non-corrupting: the old
    value is discarded, but a real trailing comment must be preserved and a
    ``#`` glued to (or quoted inside) the value must NOT be mistaken for one.
    """
    i = 0
    n = len(rest)
    quote: str | None = None
    while i < n:
        ch = rest[i]
        if quote == "'":
            if ch == "'":
                # A doubled '' is an escaped quote inside a single-quoted scalar;
                # a lone ' closes the scalar.
                if i + 1 < n and rest[i + 1] == "'":
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if quote == '"':
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                quote = None
            i += 1
            continue
        # Not inside a quoted scalar.
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "#" and i > 0 and rest[i - 1] in (" ", "\t"):
            # Genuine trailing comment. Fold the leading whitespace run into it.
            j = i
            while j > 0 and rest[j - 1] in (" ", "\t"):
                j -= 1
            return rest[:j], rest[j:]
        i += 1
    # No comment. Peel any trailing whitespace (incl. \r for CRLF files) into the
    # suffix so it is re-appended after the new value. Without \r in the strip set,
    # a CRLF-terminated line's \r would be treated as part of the value and discarded
    # when the value is replaced — producing a lone-LF line among CRLF lines (P2-5).
    value = rest.rstrip(" \t\r")
    return value, rest[len(value) :]


def _find_governance_block_span(text: str) -> tuple[int, int] | None:
    """Find the start/end char positions of the governance YAML block in ``text``.

    Scans all ```yaml ... ``` fenced blocks (same approach as filesystem.py line 504)
    and returns the span of the FIRST block whose safe_load() root key is
    ``governance:``. Returns None if no such block is found.

    Returns:
        (block_body_start, block_body_end) character indices into ``text``, where
        the body is the content BETWEEN the opening ``` and closing ```. This
        includes the leading newline after the fence.
    """
    # Pattern captures the ENTIRE fenced block including fences.
    for m in re.finditer(r"```yaml\s*\n(.*?)```", text, re.DOTALL):
        block_body = m.group(1)
        try:
            parsed = yaml.safe_load(block_body)
        except yaml.YAMLError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get(_GOVERNANCE_KEY), dict):
            body_start = m.start(1)
            body_end = m.end(1)
            return body_start, body_end
    return None


def _edit_governance_block(text: str, schema_key: str, new_value: str | None) -> str:
    """Apply a surgical in-block value edit to the governance YAML block.

    Locates the governance YAML block within ``text``, then replaces only the
    targeted scalar value at indent-2. Preserves:
    - all prose outside the block byte-for-byte
    - all untargeted keys inside the block including inline comments and key order
    - the trailing inline comment on the targeted line (M2)

    Flat scalars are at indent-2 (exactly two leading spaces). Sub-block header
    keys (review:, risk:, sources:, actions:) are also at indent-2 but are
    always dicts, not scalars — the PR1 allowlist guard ensures we never target
    them here. Comment lines (first non-whitespace char is ``#``) are skipped.

    Args:
        text: full governance.md file content.
        schema_key: the underscore-form schema key to update.
        new_value: the new scalar value string, or None to write YAML ``null``.

    When the targeted key is ABSENT from the block (a partial / hand-authored
    governance.md, which the AgentRegistryBackend layer accepts as
    ``present_valid``), the key is INSERTED as a new top-level scalar directly
    under the ``governance:`` root line rather than raising. This is what the
    verb is for: filling in partial governance records and supporting the
    implicit ``updated_at`` auto-stamp on files that omit it.

    Returns:
        Updated file content with only the targeted line's value changed (or the
        new scalar inserted when the key was absent).

    Raises:
        ValueError: if the governance YAML block (or its ``governance:`` root
            line) is not found in ``text``.
    """
    span = _find_governance_block_span(text)
    if span is None:
        raise ValueError(
            "No governance YAML block found in governance.md. "
            "The file must contain a ```yaml block with a 'governance:' root key."
        )

    body_start, body_end = span
    block_body = text[body_start:body_end]

    # Serialise the replacement scalar as valid YAML (bare when plain-safe,
    # single-quote-escaped otherwise) so the write round-trips losslessly (M2).
    yaml_value_str = _emit_yaml_scalar(new_value)

    # Match the target line at exactly 2-space indent, capturing the key prefix
    # (``  key:``) and everything after the colon separately. The value/comment
    # split is then done by ``_split_scalar_and_comment`` — a quote- and
    # whitespace-aware splitter — NOT by regex, because a ``#`` is a YAML comment
    # only when preceded by whitespace and outside quotes. A single matcher now
    # handles BOTH value-present (``  key: old``) and empty-value (``  key:`` /
    # ``  key:  # fill``) lines, so the old value is replaced without ever fusing
    # it to a trailing comment (M2 surgical preservation).
    #
    # Guards:
    # (a) Comment lines (first non-whitespace char is '#') are never target lines.
    # (b) Exactly 2 spaces of indent (flat scalar depth); nested keys at deeper
    #     indent do not match, and sub-block header keys (review:/risk:/...) are
    #     never passed as schema_key (PR1 allowlist is flat scalars only).
    # (c) Key must equal schema_key exactly.
    lines = block_body.split("\n")
    # Detect the block's line terminator for CRLF-fidelity on inserted lines (P2-5).
    # If ANY non-empty line ends with \r the block is CRLF-authored; inserted lines
    # must carry the same \r so they don't become lone-LF lines in a CRLF file.
    _crlf_eol = "\r" if any(ln.endswith("\r") for ln in lines if ln) else ""
    new_lines = []
    found = False

    key_line_pattern = re.compile(r"^(  " + re.escape(schema_key) + r":)(.*)$")

    # Quoted-key guard (F1 — audit integrity). YAML allows a mapping key in three
    # spellings: unquoted (  owner:), double-quoted (  "owner":), and single-quoted
    # (  'owner':). ``key_line_pattern`` only matches the unquoted form. When a
    # quoted form is present:
    #   (a) the duplicate-guard below misses it — match_count counts 0 unquoted
    #       occurrences and falls through to the absent-key insert path.
    #   (b) the insert path appends a NEW unquoted ``owner:`` line AFTER the
    #       quoted key — producing TWO logical copies of the key.
    #   (c) PyYAML keeps the LATER key (the quoted original). The edit lands on
    #       the newly-inserted dead copy; the audit claims ``after: new`` but the
    #       effective YAML value on re-read is still the old quoted one.
    # Refuse cleanly: the file needs manual cleanup so the CLI can safely edit it.
    key_dquoted_re = re.compile(r'^  "' + re.escape(schema_key) + r'":')
    key_squoted_re = re.compile(r"^  '" + re.escape(schema_key) + r"':")
    quoted_count = sum(
        1
        for line in lines
        if not line.lstrip().startswith("#")
        and (key_dquoted_re.match(line) or key_squoted_re.match(line))
    )
    if quoted_count > 0:
        raise ValueError(
            f"governance key {schema_key!r} is quoted; not surgically settable via CLI "
            "— edit governance.md directly"
        )

    # Duplicate-key guard. PyYAML's safe_load silently keeps the LAST duplicate of
    # a mapping key, but this surgical editor rewrites only the FIRST match (it
    # stops after ``found``). On a hand-authored file with the key repeated at
    # indent-2, the registry/validator would read the second occurrence while the
    # edit lands on the first — the change silently does NOT take effect on
    # re-read, yet the audit trail would claim it was applied. Refuse rather than
    # produce a lying audit record (principles #5 + #8). Comment lines never count.
    match_count = sum(
        1
        for line in lines
        if not line.lstrip().startswith("#") and key_line_pattern.match(line)
    )
    if match_count > 1:
        raise ValueError(
            f"Duplicate {schema_key!r} key ({match_count} occurrences) in the "
            "governance YAML block — refusing to edit an ambiguous file. Remove "
            "the duplicate in governance.md and retry."
        )

    for line in lines:
        stripped = line.lstrip()
        # Skip comment lines (first non-whitespace is '#').
        if stripped.startswith("#"):
            new_lines.append(line)
            continue

        if not found:
            m = key_line_pattern.match(line)
            if m:
                prefix = m.group(1)  # ``  key:``
                rest = m.group(2)  # everything after the colon (value + comment)
                value_part, comment_suffix = _split_scalar_and_comment(rest)
                # Preserve the operator's original spacing after the colon; when
                # there is no value (empty / comment-only line) fall back to a
                # single space so the value is never fused to the key or comment.
                value_stripped = value_part.lstrip(" \t")
                leading_ws = value_part[: len(value_part) - len(value_stripped)]
                sep = leading_ws if leading_ws else " "
                new_lines.append(f"{prefix}{sep}{yaml_value_str}{comment_suffix}")
                found = True
                continue

        new_lines.append(line)

    if not found:
        # Key is absent from the block — INSERT it as a new top-level scalar
        # directly under the ``governance:`` root line. Supports partial /
        # hand-authored files and the implicit updated_at auto-stamp on files
        # that omit it (the surgical editor must not refuse a file the registry
        # accepts as present_valid).
        new_lines = []
        inserted = False
        for line in lines:
            new_lines.append(line)
            if not inserted and line.rstrip() == f"{_GOVERNANCE_KEY}:":
                # Append the block's own line terminator (\r for CRLF, "" for LF)
                # so the inserted line does not become a lone-LF line in a CRLF block.
                new_lines.append(f"  {schema_key}: {yaml_value_str}{_crlf_eol}")
                inserted = True
        if not inserted:
            raise ValueError(
                f"Cannot insert {schema_key!r}: no '{_GOVERNANCE_KEY}:' root line "
                "found in the governance YAML block."
            )

    new_block_body = "\n".join(new_lines)
    return text[:body_start] + new_block_body + text[body_end:]


# ── Governance file read/create ────────────────────────────────────────────────


def _read_or_create_governance(governance_path: Path, agent_id: str) -> str:
    """Read existing governance.md or render the template stub if absent.

    This is the create-absent path. Per spec/55 ruling ``create-absent-governance``
    Option A: when governance.md is absent, render the init governance.md template
    via the SHARED renderer in ``atomic_agents.init`` and return the rendered string.

    MUST NOT clobber an existing file: this function chooses the base content once.
    The write decision is made at S2 step 4; this function only reads / renders.

    Args:
        governance_path: absolute path to the agent's governance.md.
        agent_id: used for template substitution when creating a stub.

    Returns:
        Current or newly-rendered governance.md content.
    """
    if governance_path.exists():
        # Use newline="" to disable Python's universal-newline translation so
        # CRLF-authored governance.md files are read with \r\n intact. The
        # surgical editor (_edit_governance_block) is CRLF-aware and must
        # receive the original bytes; Path.read_text() silently normalises
        # \r\n → \n before the content reaches the editor, making the CRLF
        # handling unreachable through the CLI (spec/55 M2 byte-fidelity).
        with governance_path.open("r", encoding="utf-8", newline="") as _f:
            return _f.read()

    # governance.md is absent — render the canonical stub. The "one canonical
    # shape" invariant is held by a byte-identity lint test across the four
    # template copies (see render_governance_stub's docstring), not by a single
    # shared call site: init/wizard.py renders its per-type copy while govern
    # renders the top-level copy through this function (spec/55 ruling).
    from ..init import render_governance_stub  # noqa: PLC0415

    return render_governance_stub(agent_id)


def _extract_governance_dict_from_text(text: str) -> dict | None:
    """Extract the raw governance dict from file text (for 'before' audit values).

    Uses PyYAML as reader/validator only (M2). Returns the raw parsed dict for the
    governance YAML block, or None if no governance block is found. Does not
    perform enum validation — this is intentional (the 'before' capture may contain
    currently-invalid values if the file was hand-edited).
    """
    for block in re.findall(r"```yaml\s*\n(.*?)```", text, re.DOTALL):
        try:
            parsed = yaml.safe_load(block)
        except yaml.YAMLError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get(_GOVERNANCE_KEY), dict):
            return parsed[_GOVERNANCE_KEY]
    return None


def _coerce_for_audit(value: Any) -> Any:
    """Coerce PyYAML-parsed values to JSON-serialisable types for audit extra{}.

    PyYAML coerces unquoted ISO dates to datetime.date objects and bare yes/no to
    bools. Both are not JSON-serialisable. Normalise them here before placing into
    RunRecord.extra{} (spec/55 P2 prep finding).
    """
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bool):
        return "yes" if value else "no"
    return value


# ── --show display ─────────────────────────────────────────────────────────────


def _show_governance(ref: Any, use_json: bool) -> int:
    """Display the current governance record for ``--show``."""
    gov = ref.governance
    has_gov = ref.has_governance

    if use_json:
        # Determine governance_state from AgentRef fields.
        if not has_gov:
            gov_state = "absent"
        elif gov is None:
            gov_state = "present_no_block"
        elif gov.parse_errors:
            gov_state = "present_invalid"
        else:
            gov_state = "present_valid"

        parse_errors = list(gov.parse_errors) if gov and gov.parse_errors else []
        gov_dict = gov.to_dict() if (gov and not gov.parse_errors) else None

        print(
            json.dumps(
                {
                    "ok": True,
                    "agent": ref.id,
                    "has_governance": has_gov,
                    "governance_state": gov_state,
                    "parse_errors": parse_errors,
                    "governance": gov_dict,
                },
                indent=2,
            )
        )
        return 0

    # Human-readable display.
    if not has_gov:
        print(f"[{ref.id}] governance.md: absent")
        return 0

    if gov is None:
        print(f"[{ref.id}] governance.md: present (no governance: YAML block found)")
        return 0

    if gov.parse_errors:
        print(f"[{ref.id}] governance.md: PRESENT_INVALID")
        for err in gov.parse_errors:
            print(f"  Parse error: {err}")
        print("  (other fields are null due to parse failure)")
        return 0

    print(f"[{ref.id}] governance:")
    d = gov.to_dict()
    for k, v in d.items():
        if k == "parse_errors":
            continue
        print(f"  {k}: {v!r}")
    return 0


# ── --json structured output helpers ──────────────────────────────────────────


def _json_change_list(changes: list[dict]) -> list[dict]:
    """Build the ``--json`` ``changes`` payload from the internal change dicts.

    before/after are redacted at THIS (machine-readable) echo site — the same
    redact-at-echo rule the persisted audit and the console preview follow (M8) —
    so a copilot never reads a secret-shaped value in the clear.
    """
    return [
        {
            "field": c["field"],
            "before": _redact_if_secret(c["field"], c["before"]),
            "after": _redact_if_secret(c["field"], c["after"]),
        }
        for c in changes
    ]


def _emit_json_error(error_type: str, reason: str) -> None:
    """Emit a structured JSON refusal to stdout."""
    print(
        json.dumps({"ok": False, "error_type": error_type, "reason": reason}, indent=2)
    )


def _emit_json_success(
    agent_id: str,
    changes: list[dict],
    snapshot_path: str | None,
    audit_status: str,
    dry_run: bool = False,
) -> None:
    """Emit a structured JSON success payload to stdout."""
    payload: dict = {
        "ok": True,
        "agent": agent_id,
        "changes": changes,
    }
    if dry_run:
        payload["dry_run"] = True
    # F3: always include snapshot_path — null when absent (create-absent or dry-run)
    # so a copilot can do payload["snapshot_path"] without risking KeyError.
    payload["snapshot_path"] = snapshot_path
    payload["audit_status"] = audit_status
    print(json.dumps(payload, indent=2))


# ── Confirm gate (S2 step 3) — shared by --set and --restore ──────────────────


def _emit_abort(use_json: bool, *, interrupted: bool) -> None:
    """Emit the abort refusal (S2 step 3 decline / SIGINT).

    ``error_type`` stays ``'aborted'`` for BOTH cases (decline and SIGINT) —
    only the exit code and the human-readable reason distinguish them (spec/55
    exit-code ladder normative note).
    """
    reason = (
        "operator interrupted (SIGINT); no changes written"
        if interrupted
        else "operator declined the confirmation; no changes written"
    )
    if use_json:
        print(
            json.dumps(
                {"ok": False, "error_type": "aborted", "reason": reason}, indent=2
            )
        )
    else:
        msg = "\nInterrupted." if interrupted else "Aborted."
        print(msg, file=sys.stderr)


def _require_confirmation(use_json: bool, yes: bool) -> int | None:
    """S2 step 3 confirm gate. Returns ``None`` to proceed, or an exit code.

    Exit-code ladder (spec/55 normative note):
      1   — non-interactive without ``--yes`` (a VALIDATION-style refusal,
            not a decline — the operator never got to decide)
      3   — interactive 'n' / EOF decline (``error_type`` stays ``'aborted'``;
            exit 2 is reserved for argparse's own usage-error code, so a
            copilot must be able to tell "bad flags" apart from "declined")
      130 — KeyboardInterrupt / SIGINT (POSIX 128+2 convention)

    MUST be called AFTER preview/dry-run and BEFORE the manage lease is
    acquired (spec/55 M11 note) — an idle TTY prompt must never hold the
    per-agent lease, or every other manage write on the agent starves until
    the human answers.
    """
    if yes:
        return None

    if not sys.stdin.isatty():
        reason = (
            "--yes is required for non-interactive use "
            "(no TTY detected; use --yes to apply)."
        )
        if use_json:
            _emit_json_error("confirmation_required", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    # Interactive confirm on a TTY. Write the prompt to stderr (not input()'s
    # default stdout) so that under --json stdout carries ONLY the machine-
    # readable JSON — a copilot driver on a real TTY without --yes must never
    # see the human prompt text prepended to the JSON stream (S3 contract).
    try:
        print("Apply these changes? [y/N] ", end="", file=sys.stderr, flush=True)
        answer = input().strip().lower()
    except EOFError:
        _emit_abort(use_json, interrupted=False)
        return 3
    except KeyboardInterrupt:
        _emit_abort(use_json, interrupted=True)
        return 130

    if answer not in ("y", "yes"):
        _emit_abort(use_json, interrupted=False)
        return 3

    return None


# ── Restore audit-shape helper (#710) ──────────────────────────────────────────


def _diff_governance_field_names(before: dict | None, after: dict | None) -> list[str]:
    """Field-level diff between two raw governance dicts (restore audit shape, #710).

    Restricted to the PR1 flat-scalar allowlist — the same fields ``--set``
    can target — so restore's ``changed_fields`` shares the exact shape
    ``govern --set`` already emits (a list of ``GovernanceRecord`` scalar
    keys), matching the M8-pinned per-field audit shape every verb must
    share (P1 prep finding: restore must NOT compute a whole-file diff).
    Coerces both sides via ``_coerce_for_audit`` first so a YAML-parsed
    ``datetime.date`` / ``bool`` does not spuriously differ from its
    persisted string form.
    """
    before = before or {}
    after = after or {}
    changed: list[str] = []
    for key in _FLAT_SCALAR_FIELDS:
        if _coerce_for_audit(before.get(key)) != _coerce_for_audit(after.get(key)):
            changed.append(key)
    return changed


def _principal_id() -> str:
    """Resolve the audit identity (spec/48 PrincipalBackend, home-user default).

    PR1 LIMITATION (deferred): a CLI invocation has no verified-claims identity
    transport — Principal derivation is an HTTP/serve-layer concern (spec/48).
    The management audit identity is therefore always LOCAL_PRINCIPAL, which is
    the correct home-user stamp. Multi-operator identity resolution for the CLI
    (so a shared-host fleet audit carries WHICH operator ran the command) is a
    follow-up; it slots in here by resolving the configured principal backend.
    """
    try:
        from ..principal import LocalPrincipalBackend  # noqa: PLC0415

        principal = LocalPrincipalBackend().derive_principal(None)
        return principal.identifier
    except Exception:  # noqa: BLE001
        return "local"  # fallback per spec/48 (LOCAL_PRINCIPAL identifier)


def _relative_snapshot_path(snapshot_path: Path | None, agent_dir: Path) -> str | None:
    """Snapshot path relative to agent_dir for portability, or None when absent."""
    if snapshot_path is None:
        return None
    try:
        return str(snapshot_path.relative_to(agent_dir))
    except ValueError:
        return str(snapshot_path)


# ── Main verb entry point ──────────────────────────────────────────────────────


def run_govern(args: Any, agents_root: Path) -> int:
    """Entry point for ``atomic-agents manage govern <agent> ...``.

    Dispatches to ``--show`` (read-only), ``--list-snapshots`` (read-only,
    #710), ``--restore <snapshot-id>`` (#710 — the FULL S2 five-step routine
    through the hoisted spine, not a bypass), or the ``--set`` field-edit
    path (S2 five-step safety routine, spec/55).

    Args:
        args: parsed argparse namespace.
        agents_root: resolved fleet root (from --agents-root or env default).

    Returns:
        Process exit-code ladder (spec/55 normative note):
          0   — applied success / dry-run preview / --show / --list-snapshots
          1   — refusal (validation, registry, path, lock-backend-unavailable,
                agent-busy — the latter two raised as exceptions and caught
                CENTRALLY by ``run_manage()``, not here) or a write/read error
          3   — interactive decline ('n' / EOF) — ``error_type`` stays 'aborted'
          130 — KeyboardInterrupt / SIGINT
    """
    use_json = getattr(args, "json", False)
    dry_run = getattr(args, "dry_run", False)
    yes = getattr(args, "yes", False)
    show = getattr(args, "show", False)
    list_snapshots_flag = getattr(args, "list_snapshots", False)
    restore_id = getattr(args, "restore", None)
    set_pairs: list[str] = list(getattr(args, "set", None) or [])
    agent_id: str = args.agent

    # ── List-mutation refusal (spec/55 grammar-pinning) ───────────────────────
    # --add / --remove / --set-json are recognised by the parser but unimplemented
    # in PR1. Return a clean structured refusal (respecting --json) rather than an
    # argparse ``unrecognized arguments`` error, so the flag's parse status is
    # stable across PRs and the --json contract holds. Fires before the registry
    # resolve — it is a scope refusal independent of whether the agent exists.
    for flag_name, dest in (
        ("--add", "add"),
        ("--remove", "remove"),
        ("--set-json", "set_json"),
    ):
        values = getattr(args, dest, None) or []
        if values:
            exc = ManageListMutationRefused(flag_name, values[0])
            if use_json:
                _emit_json_error(exc.error_type, str(exc))
            else:
                print(f"Error: {exc}", file=sys.stderr)
            return 1

    # ── Single-primary-action refusal (#710 restore-cli-surface ruling) ──────
    # --restore is mutually exclusive with --set (the ruling's explicit
    # requirement) and, by the same single-primary-action intent, with --show
    # / --list-snapshots too — each of the four is its own complete command.
    primary_actions = [
        name
        for name, present in (
            ("--restore", bool(restore_id)),
            ("--set", bool(set_pairs)),
            ("--show", show),
            ("--list-snapshots", list_snapshots_flag),
        )
        if present
    ]
    if len(primary_actions) > 1:
        reason = (
            f"{' and '.join(primary_actions)} are mutually exclusive — pass "
            "exactly one primary action per invocation."
        )
        if use_json:
            _emit_json_error("multiple_primary_actions", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    # ── S1: Resolve through AgentRegistryBackend ──────────────────────────────
    try:
        from ..agent_registry import get_default_agent_registry_backend  # noqa: PLC0415

        registry = get_default_agent_registry_backend(agents_root)
        ref = registry.get_agent(agent_id)
    except Exception as exc:  # noqa: BLE001
        reason = f"Failed to load agent registry: {exc}"
        if use_json:
            _emit_json_error("registry_error", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    if ref is None:
        reason = f"Agent {agent_id!r} not found in the registry at {agents_root}"
        if use_json:
            _emit_json_error("agent_not_found", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    # S1: derive write target exclusively from ref.location (M7, symlink-safe).
    agent_dir = Path(ref.location)

    # Additional containment guard at write time (spec/55 P1 prep finding).
    try:
        safe_resolve_under(agent_dir, agents_root)
    except Exception as exc:  # noqa: BLE001
        reason = f"Agent directory outside agents_root — refused: {exc}"
        if use_json:
            _emit_json_error("path_traversal", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    # ── Read-only paths (--show / --list-snapshots) — NEVER touch the manage
    # lease (spec/55 M11 note: reads must not contend with writes) ───────────
    if show:
        return _show_governance(ref, use_json)

    if list_snapshots_flag:
        return _list_snapshots(agent_id, agent_dir, use_json)

    # ── Write paths (--set / --restore) resolve the governance.md target.
    # The manage lock backend is constructed PER-VERB, inside _run_set /
    # _run_restore, AFTER their --dry-run early-exit (spec/55 M11 fix,
    # adversarial review: --dry-run must never construct the lock backend,
    # or a misconfigured/unreachable LockBackend fails a preview that never
    # intended to touch the lease). The real write path still constructs it
    # before run_managed_write, so a broken LockBackend refuses BEFORE any
    # write is attempted.
    governance_path = agent_dir / "governance.md"

    # Containment guard on the governance file path itself (P1 prep finding).
    try:
        safe_resolve_under(governance_path, agents_root)
    except Exception as exc:  # noqa: BLE001
        reason = f"governance.md path outside agents_root — refused: {exc}"
        if use_json:
            _emit_json_error("path_traversal", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    # Symlink check on governance.md write target (mirrors filesystem.py line 456).
    if governance_path.exists() and governance_path.is_symlink():
        reason = (
            f"governance.md at {governance_path} is a symlink — write refused "
            "(path containment guard)."
        )
        if use_json:
            _emit_json_error("symlink_refused", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    if restore_id:
        return _run_restore(
            agent_id=agent_id,
            agent_dir=agent_dir,
            agents_root=agents_root,
            governance_path=governance_path,
            snapshot_id=restore_id,
            use_json=use_json,
            dry_run=dry_run,
            yes=yes,
        )

    # ── Require at least one --set ────────────────────────────────────────────
    if not set_pairs:
        reason = (
            "No primary action specified. Use --set field=value, --show, "
            "--list-snapshots, or --restore <snapshot-id>."
        )
        if use_json:
            _emit_json_error("no_fields", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    return _run_set(
        agent_id=agent_id,
        agent_dir=agent_dir,
        agents_root=agents_root,
        governance_path=governance_path,
        ref=ref,
        set_pairs=set_pairs,
        use_json=use_json,
        dry_run=dry_run,
        yes=yes,
    )


# ── --list-snapshots (#710, read-only) ─────────────────────────────────────────


def _list_snapshots(agent_id: str, agent_dir: Path, use_json: bool) -> int:
    """``--list-snapshots`` — read-only listing of the govern snapshot dir (#710).

    Symmetric with ``--show``: a pure read, never acquires the manage lease
    (spec/55 M11 note — reads never contend with writes).
    """
    snapshots = list_snapshots(agent_dir, _SNAPSHOT_SUBDIR)
    ids = [p.name for p in snapshots]

    if use_json:
        print(json.dumps({"ok": True, "agent": agent_id, "snapshots": ids}, indent=2))
        return 0

    if not ids:
        print(f"[{agent_id}] no governance snapshots.")
        return 0

    print(f"[{agent_id}] governance snapshots ({len(ids)}):")
    for sid in ids:
        print(f"  {sid}")
    return 0


# ── --set field-edit path (S2 five-step routine through the hoisted spine) ────


def _run_set(
    *,
    agent_id: str,
    agent_dir: Path,
    agents_root: Path,
    governance_path: Path,
    ref: Any,
    set_pairs: list[str],
    use_json: bool,
    dry_run: bool,
    yes: bool,
) -> int:
    """``manage govern <agent> --set field=value ...`` — the original verb (#609).

    Steps 1-3 (validate/preview/confirm) run against an ADVISORY pre-lock
    read of governance.md (for dry-run/preview display + doomed-edit parity,
    M6/M7). Step 4/4b (snapshot + atomic write) run through the hoisted
    ``run_managed_write`` spine helper, which re-reads governance.md FRESH
    *inside* the per-agent manage lease — the P0 fix for the lost-update
    race (#709): a base read before an interactive confirm prompt (which can
    block indefinitely) must never be reused as the write base, or two
    concurrent writers can silently clobber each other. ``--set`` values are
    absolute (not deltas), so re-applying the same parsed field tokens
    against the fresh base is safe and needs no extra CAS logic.
    """
    # ── S2 Step 1: Validate ───────────────────────────────────────────────────

    # PRESENT_INVALID guard: refuse if existing governance.md has parse errors.
    # The surgical editor cannot safely operate on a malformed block (spec/55 P1).
    if (
        ref.has_governance
        and ref.governance is not None
        and ref.governance.parse_errors
    ):
        exc = ManageGovernanceInvalidError(agent_id, ref.governance.parse_errors)
        if use_json:
            _emit_json_error(exc.error_type, str(exc))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1

    # Parse and validate all --set tokens.
    parsed_fields: list[tuple[str, str | None]] = []
    for token in set_pairs:
        try:
            schema_key, value = _parse_set_token(token)
        except ManageNestedPathRefused as exc:
            if use_json:
                _emit_json_error(exc.error_type, str(exc))
            else:
                print(f"Error: {exc}", file=sys.stderr)
            return 1
        except ManageUnknownFieldError as exc:
            if use_json:
                _emit_json_error(exc.error_type, str(exc))
            else:
                print(f"Error: {exc}", file=sys.stderr)
            return 1
        except ValueError as exc:
            if use_json:
                _emit_json_error("validation_error", str(exc))
            else:
                print(f"Error: {exc}", file=sys.stderr)
            return 1

        # Normalise tristate spelling (true/false -> yes/no).
        if schema_key in ("customer_data", "writes_sor"):
            value = _normalise_tristate(value)

        try:
            _validate_field_value(schema_key, value)
        except (
            ManageControlCharRefused,
            ManageInvalidEnumError,
            ManageInvalidDateError,
        ) as exc:
            if use_json:
                _emit_json_error(exc.error_type, str(exc))
            else:
                print(f"Error: {exc}", file=sys.stderr)
            return 1

        parsed_fields.append((schema_key, value))

    # Check for operator-supplied updated_at (don't double-stamp if explicit).
    explicit_updated_at = any(k == "updated_at" for k, _ in parsed_fields)

    # Auto-stamp updated_at on every applied write (spec/55 P1 prep finding —
    # always implicit; the before/after in audit records reflects the auto-stamp).
    if not explicit_updated_at:
        parsed_fields.append(("updated_at", _today_iso()))

    # ── Advisory pre-lock read (preview/dry-run only — see docstring) ────────
    # A present-but-unreadable governance.md (e.g. chmod 000) reaches here via the
    # create-absent branch — the registry classifies PRESENT_UNREADABLE the same
    # as ABSENT, but the file exists() so _read_or_create_governance reads it. A
    # bare read would raise an uncaught PermissionError and print a traceback,
    # breaking the S3 --json structured-refusal contract. Catch and refuse cleanly.
    try:
        preview_content = _read_or_create_governance(governance_path, agent_id)
    except OSError as exc:
        reason = f"Could not read governance.md: {exc}"
        if use_json:
            _emit_json_error("read_error", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1
    preview_gov_dict = _extract_governance_dict_from_text(preview_content)

    # ── S2 Step 2: Preview ────────────────────────────────────────────────────
    changes: list[dict] = []
    for schema_key, new_value in parsed_fields:
        before_raw = (preview_gov_dict or {}).get(schema_key)
        before = _coerce_for_audit(before_raw)
        after = new_value  # None == null

        display_before = _redact_if_secret(
            schema_key, str(before) if before is not None else "null"
        )
        display_after = _redact_if_secret(
            schema_key, str(new_value) if new_value is not None else "null"
        )

        changes.append(
            {
                "field": schema_key,
                "before": before,
                "after": after,
                "display_before": display_before,
                "display_after": display_after,
            }
        )

    if not use_json:
        # Human-readable preview.
        print(f"\n[{agent_id}] governance.md changes:")
        for c in changes:
            print(f"  {c['field']}: {c['display_before']!r} -> {c['display_after']!r}")
        print()

    # ── S2 Step 3 (pre-exit): Compute the edited content against the ADVISORY
    # pre-lock read ────────────────────────────────────────────────────────
    # Pure computation — no writes, no I/O side-effects. Running this BEFORE the
    # dry-run exit means a doomed edit (no governance block in the file, duplicate
    # key) fails with the same edit_error refusal on BOTH --dry-run and apply.
    # Without this ordering, --dry-run reports {ok:true, changes:[...]} and the
    # subsequent apply refuses — a preview-as-artifact break (M6/M7, P2-3 fix).
    # This is ADVISORY (dry-run/preview parity only) — the AUTHORITATIVE edit
    # that actually gets written is recomputed fresh, under the lock, below.
    try:
        for schema_key, new_value in parsed_fields:
            preview_content = _edit_governance_block(
                preview_content, schema_key, new_value
            )
    except (ValueError, KeyError) as exc:
        reason = f"Failed to apply --set edit: {exc}"
        if use_json:
            _emit_json_error("edit_error", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    # ── S2 Step 3: --dry-run exits here (MUST precede --yes check) ───────────
    if dry_run:
        if use_json:
            _emit_json_success(
                agent_id,
                _json_change_list(changes),
                snapshot_path=None,
                audit_status="n/a",
                dry_run=True,
            )
        else:
            print("[dry-run] No changes written.")
        return 0

    # ── S2 Step 3: Confirm (BEFORE the manage lease is ever acquired) ────────
    confirm_exit = _require_confirmation(use_json, yes)
    if confirm_exit is not None:
        return confirm_exit

    # ── S2 Steps 4/4b: hoisted spine — lock, FRESH read, snapshot, write ─────
    def _read_base() -> tuple[str, bool]:
        file_existed = governance_path.exists()
        content = _read_or_create_governance(governance_path, agent_id)
        return content, file_existed

    def _apply_edit(fresh_content: str) -> str:
        new_content = fresh_content
        for schema_key, new_value in parsed_fields:
            new_content = _edit_governance_block(new_content, schema_key, new_value)
        return new_content

    # spec/55 M11 fix: construct the lock backend HERE — after --dry-run's
    # early exit above and after the confirm gate — so a misconfigured/
    # unreachable LockBackend never blocks a preview. The real write path
    # still constructs it before run_managed_write is called.
    lock_backend = get_manage_lock_backend(agent_dir)

    try:
        result: ManagedWriteResult = run_managed_write(
            agent_dir=agent_dir,
            agent_id=agent_id,
            write_path=governance_path,
            subdir=_SNAPSHOT_SUBDIR,
            read_base=_read_base,
            apply_edit=_apply_edit,
            lock_backend=lock_backend,
        )
    except (ManageAgentBusyError, ManageLockUnavailableError):
        # Caught CENTRALLY by run_manage() — propagate uncaught (spec/55 M11
        # ruling: these are spine-level, not per-verb, refusals).
        raise
    except (ValueError, KeyError) as exc:
        reason = f"Failed to apply --set edit against the current governance.md: {exc}"
        if use_json:
            _emit_json_error("edit_error", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1
    except OSError as exc:
        reason = f"Failed to write governance.md: {exc}"
        if use_json:
            _emit_json_error("write_error", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 -- fail-closed structured refusal (Fix 5,
        # adversarial review): run_managed_write's snapshot/write calls can raise
        # broader than ValueError/KeyError/OSError (its own docstring says so) —
        # an uncaught exception here must never surface a raw traceback and break
        # the S3 --json structured-refusal contract; matches the pre-hoist
        # (pre-#709) broad-except posture this hoist had narrowed away.
        reason = f"Unexpected error while writing governance.md: {exc}"
        if use_json:
            _emit_json_error("write_error", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    # ── S2 Step 5: Audit (lease already released — AFTER write, non-fatal) ──
    # Recomputed from the FRESH (in-lock) read, not the advisory pre-lock
    # preview — the P0 fix: audit before/after must reflect the actual disk
    # state the write was based on, not a possibly-stale preview snapshot.
    fresh_gov_dict = _extract_governance_dict_from_text(result.prior_content)
    audit_changed_fields = [f for f, _ in parsed_fields]
    audit_before: dict[str, Any] = {}
    audit_after: dict[str, Any] = {}
    for schema_key, new_value in parsed_fields:
        before_val = _coerce_for_audit((fresh_gov_dict or {}).get(schema_key))
        audit_before[schema_key] = _cap_for_audit(
            _redact_if_secret(schema_key, before_val)
        )
        audit_after[schema_key] = _cap_for_audit(
            _redact_if_secret(schema_key, new_value)
        )

    rel_snapshot = _relative_snapshot_path(result.snapshot_path, agent_dir)
    principal_id = _principal_id()

    record = RunRecord(
        ts=datetime.now(timezone.utc).isoformat(),
        run_id=str(uuid.uuid4()),
        primitive=PRIMITIVE_MANAGE_GOVERN,
        status="applied",
        summary=f"manage govern {agent_id}: set {', '.join(audit_changed_fields)}"[
            :200
        ],
        model="n/a",
        input_tokens=0,
        output_tokens=0,
        # cost_usd=None (omitted — not an LLM call; spec/55 M8 P2 prep finding)
        agent_name=agent_id,  # required for fleet-level LogQuery(agent_name=...) queries
        extra={
            "principal_id": principal_id,
            "changed_fields": audit_changed_fields,
            "before": audit_before,
            "after": audit_after,
            "snapshot_path": rel_snapshot,
            # True when this write CREATED governance.md (no restorable prior
            # state; snapshot_path is null). A rollback tool reads this to know
            # the prior state was 'file absent', not a stub.
            "created": not result.file_existed,
        },
    )

    audit_ok, audit_warnings = append_management_audit(record, agent_dir, agents_root)

    # ── Success output ────────────────────────────────────────────────────────
    audit_status = "ok" if audit_ok else "warn"

    # Rebuild the --json changes payload from the AUTHORITATIVE (fresh, in-lock)
    # before values, not the advisory pre-lock preview — same rationale as the
    # audit record above (P0 fix).
    authoritative_changes = [
        {"field": k, "before": audit_before[k], "after": audit_after[k]}
        for k in audit_changed_fields
    ]

    if use_json:
        _emit_json_success(
            agent_id,
            authoritative_changes,
            snapshot_path=rel_snapshot,
            audit_status=audit_status,
        )
    else:
        print(f"[{agent_id}] governance.md updated.")
        if rel_snapshot is not None:
            print(f"  Snapshot: {rel_snapshot}")
        else:
            print("  Created governance.md (no prior file; no snapshot).")
        if not audit_ok:
            for w in audit_warnings:
                print(f"  {w}")

    return 0


# ── --restore path (#710 — full S2 five-step routine, NOT a bypass) ───────────


def _run_restore(
    *,
    agent_id: str,
    agent_dir: Path,
    agents_root: Path,
    governance_path: Path,
    snapshot_id: str,
    use_json: bool,
    dry_run: bool,
    yes: bool,
) -> int:
    """``manage govern <agent> --restore <snapshot-id>`` — rollback verb (#710).

    Runs the FULL S2 five-step routine through the SAME hoisted spine govern
    --set uses (validate -> preview -> confirm -> snapshot+write -> audit) —
    NOT a bypass. In particular, restore ITSELF snapshots the current
    (pre-restore) governance.md before overwriting it, so a restore is
    always itself undoable via a second restore.

    Restore-validate-semantics (maintainer ruling): validation here means the
    snapshot's governance block PARSES + the preview diff is shown — NOT a
    full re-validation against the current schema (a snapshot is by
    definition prior known-good content). The snapshot-belongs-to-this-agent
    guarantee is enforced by ``resolve_snapshot_path``'s containment check
    (the snapshot_id is resolved ONLY under the TARGET agent's own
    ``.config-snapshots/govern/`` tree — a snapshot_id that only exists under
    a different agent's tree simply does not resolve here, so cross-agent
    and genuinely-nonexistent snapshot ids hit the identical
    ``ManageSnapshotNotFoundError`` refusal; this is a deliberate
    indistinguishability, not a gap — a caller must never learn whether a
    given snapshot id exists under some OTHER agent).

    Restore semantics decision (Tier B — not covered by a maintainer ruling,
    decided here and documented so it is never a silent surprise): restore is
    a BYTE-EXACT rollback. It does NOT re-stamp ``updated_at`` the way
    ``--set`` does — the whole point of restoring a snapshot is that the
    resulting governance.md is byte-identical to the snapshotted state (M3's
    own "restorable to byte-faithful prior content" language), and an
    implicit re-stamp would silently defeat that.
    """
    # ── Resolve + validate the snapshot belongs to THIS agent ─────────────────
    try:
        snapshot_src_path = resolve_snapshot_path(
            agent_dir, _SNAPSHOT_SUBDIR, snapshot_id
        )
    except ManageSnapshotNotFoundError as exc:
        if use_json:
            _emit_json_error(exc.error_type, str(exc))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        with snapshot_src_path.open("r", encoding="utf-8", newline="") as _f:
            snapshot_content = _f.read()
    except OSError as exc:
        reason = f"Could not read snapshot {snapshot_id!r}: {exc}"
        if use_json:
            _emit_json_error("read_error", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    # Restore-validate-semantics: the snapshot's governance block MUST parse.
    # Not a full re-validation (a snapshot is by definition prior known-good
    # content) — just confirm it still parses as a governance YAML block.
    snapshot_gov_dict = _extract_governance_dict_from_text(snapshot_content)
    if snapshot_gov_dict is None:
        reason = (
            f"Snapshot {snapshot_id!r} does not contain a parseable governance "
            "YAML block — refusing to restore."
        )
        if use_json:
            _emit_json_error("restore_snapshot_invalid", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    # ── Advisory pre-lock read of CURRENT governance.md, for preview only ────
    current_exists = governance_path.exists()
    try:
        preview_current_content = (
            _read_or_create_governance(governance_path, agent_id)
            if current_exists
            else ""
        )
    except OSError as exc:
        reason = f"Could not read governance.md: {exc}"
        if use_json:
            _emit_json_error("read_error", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1
    preview_current_dict = (
        _extract_governance_dict_from_text(preview_current_content)
        if current_exists
        else None
    )

    changed_fields = _diff_governance_field_names(
        preview_current_dict, snapshot_gov_dict
    )
    preview_changes = [
        {
            "field": f,
            "before": _coerce_for_audit((preview_current_dict or {}).get(f)),
            "after": _coerce_for_audit(snapshot_gov_dict.get(f)),
        }
        for f in changed_fields
    ]

    if not use_json:
        print(f"\n[{agent_id}] restoring governance.md from snapshot {snapshot_id}:")
        if not preview_changes:
            print(
                "  (no field differences — governance.md already matches the snapshot)"
            )
        for c in preview_changes:
            print(f"  {c['field']}: {c['before']!r} -> {c['after']!r}")
        print()

    if dry_run:
        if use_json:
            _emit_json_success(
                agent_id,
                _json_change_list(preview_changes),
                snapshot_path=None,
                audit_status="n/a",
                dry_run=True,
            )
        else:
            print("[dry-run] No changes written.")
        return 0

    # ── Confirm (BEFORE the manage lease is ever acquired) ───────────────────
    confirm_exit = _require_confirmation(use_json, yes)
    if confirm_exit is not None:
        return confirm_exit

    # ── Hoisted spine — lock, FRESH read (this IS the pre-restore snapshot
    # base), snapshot, write ─────────────────────────────────────────────────
    def _read_base() -> tuple[str, bool]:
        file_existed = governance_path.exists()
        content = (
            _read_or_create_governance(governance_path, agent_id)
            if file_existed
            else ""
        )
        return content, file_existed

    def _apply_edit(_fresh_content: str) -> str:
        # Restore is an absolute overwrite — the fresh base is used only for
        # the pre-restore snapshot (taken by run_managed_write when
        # file_existed), never as an edit target.
        return snapshot_content

    # spec/55 M11 fix: construct the lock backend HERE — after --dry-run's
    # early exit above and after the confirm gate — so a misconfigured/
    # unreachable LockBackend never blocks a preview. The real write path
    # still constructs it before run_managed_write is called.
    lock_backend = get_manage_lock_backend(agent_dir)

    try:
        result: ManagedWriteResult = run_managed_write(
            agent_dir=agent_dir,
            agent_id=agent_id,
            write_path=governance_path,
            subdir=_SNAPSHOT_SUBDIR,
            read_base=_read_base,
            apply_edit=_apply_edit,
            lock_backend=lock_backend,
        )
    except (ManageAgentBusyError, ManageLockUnavailableError):
        # Caught CENTRALLY by run_manage() — propagate uncaught.
        raise
    except OSError as exc:
        reason = f"Failed to restore governance.md: {exc}"
        if use_json:
            _emit_json_error("write_error", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 -- fail-closed structured refusal (Fix 5,
        # adversarial review): matches the pre-hoist (pre-#710) broad-except
        # posture — an unexpected error from the snapshot/write calls must never
        # surface a raw traceback and break the S3 --json structured-refusal
        # contract.
        reason = f"Unexpected error while restoring governance.md: {exc}"
        if use_json:
            _emit_json_error("write_error", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    # ── Audit (lease already released — AFTER write, non-fatal) ─────────────
    # Recomputed from the FRESH (in-lock) read, not the advisory pre-lock
    # preview (P0 fix — same rationale as _run_set).
    fresh_current_dict = (
        _extract_governance_dict_from_text(result.prior_content)
        if result.file_existed
        else None
    )
    audit_changed_fields = _diff_governance_field_names(
        fresh_current_dict, snapshot_gov_dict
    )
    audit_before: dict[str, Any] = {}
    audit_after: dict[str, Any] = {}
    for key in audit_changed_fields:
        before_val = _coerce_for_audit((fresh_current_dict or {}).get(key))
        after_val = _coerce_for_audit(snapshot_gov_dict.get(key))
        audit_before[key] = _cap_for_audit(_redact_if_secret(key, before_val))
        audit_after[key] = _cap_for_audit(_redact_if_secret(key, after_val))

    # Two DISTINCT snapshot references (P1 prep finding — swap risk): the
    # SOURCE snapshot the operator asked to restore FROM (restored_from) vs.
    # the NEW pre-restore snapshot run_managed_write just took of the
    # about-to-be-overwritten current state (snapshot_path). Named distinctly
    # at construction so a swap is visually obvious in review.
    pre_restore_snapshot_path = _relative_snapshot_path(result.snapshot_path, agent_dir)
    source_snapshot_id = snapshot_id
    principal_id = _principal_id()

    record = RunRecord(
        ts=datetime.now(timezone.utc).isoformat(),
        run_id=str(uuid.uuid4()),
        primitive=PRIMITIVE_MANAGE_RESTORE,
        status="applied",
        summary=f"manage govern {agent_id}: restore from {source_snapshot_id}"[:200],
        model="n/a",
        input_tokens=0,
        output_tokens=0,
        # cost_usd=None (omitted — not an LLM call; matches govern --set's convention)
        agent_name=agent_id,
        extra={
            "principal_id": principal_id,
            "changed_fields": audit_changed_fields,
            "before": audit_before,
            "after": audit_after,
            # The PRE-RESTORE snapshot restore itself just took (of the state
            # being overwritten) — restorable via a second --restore.
            "snapshot_path": pre_restore_snapshot_path,
            # The SOURCE snapshot this restore consumed.
            "restored_from": source_snapshot_id,
            "created": not result.file_existed,
        },
    )

    # Exactly ONE RunRecord for one logical restore (manage_restore) — never a
    # second manage_govern record (P1 prep finding: restore reuses the
    # SNAPSHOT/CONFIRM/WRITE mechanics of the hoisted spine, not govern's own
    # audit-emitting code path).
    audit_ok, audit_warnings = append_management_audit(record, agent_dir, agents_root)

    audit_status = "ok" if audit_ok else "warn"
    authoritative_changes = [
        {"field": k, "before": audit_before[k], "after": audit_after[k]}
        for k in audit_changed_fields
    ]

    if use_json:
        _emit_json_success(
            agent_id,
            authoritative_changes,
            snapshot_path=pre_restore_snapshot_path,
            audit_status=audit_status,
        )
    else:
        print(
            f"[{agent_id}] governance.md restored from snapshot {source_snapshot_id}."
        )
        if pre_restore_snapshot_path is not None:
            print(f"  Pre-restore snapshot: {pre_restore_snapshot_path}")
        else:
            print("  Created governance.md (no prior file; no pre-restore snapshot).")
        if not audit_ok:
            for w in audit_warnings:
                print(f"  {w}")

    return 0

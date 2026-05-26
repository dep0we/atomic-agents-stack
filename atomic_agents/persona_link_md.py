"""Parse ``<agent>/persona.link.md`` -- the shared-persona reference file.

When an agent's persona is owned by a ``PersonaBackend`` (D1 of spec/33), the
agent's instance directory carries a ``persona.link.md`` file pointing at the
shared persona record. The file is YAML inside a markdown code block, matching
the operator-edit convention used in ``judges.md`` / ``mandates.md`` /
``policy.md`` / ``model.md``::

    # Persona link

    ```yaml
    kind: shared
    persona_id: customer-support-v3
    ```

    Optional markdown body documenting *why* this agent links to that record.

The parser is a pure function: ``text -> PersonaLink`` or ``path -> PersonaLink``.
It does no I/O beyond the path read (when given a path) and never raises
``OSError`` -- a missing or unreadable file is the caller's responsibility to
detect via ``Path.is_file()`` BEFORE calling the parser.

Module placement: top level (D-PP-5), matching the established sibling
convention for operator-edited markdown parsers (``judges_md.py``,
``mandates_md.py``, ``policy_md.py``, ``mcp.py``).

Spec: ``docs/spec/33-persona-backend.md`` §"Shared-persona reference".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .exceptions import PersonaLinkInvalid

# ──────────────────────────────────────────────────────────────────────────────
# Constants

# Supported ``kind:`` values. v1 supports only ``shared``; future expansion is
# ``template`` (operator templates), ``git`` (git-backed personas), ``vault``
# (vault-backed personas). The parser refuses unknown kinds at parse time so
# operator typos surface loudly instead of silently routing to a wrong code path.
_SUPPORTED_KINDS = frozenset({"shared"})

# persona_id charset: alphanumeric + underscore + hyphen + dot + plus + at-sign.
# Mirrors ``_PERSONA_ID_PATTERN`` in ``persona/filesystem.py:71`` and
# ``_AGENT_NAME_RE`` in ``policy_md.py:113`` -- cross-Protocol uniformity (D4).
_PERSONA_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.+@-]+$")

# Control-character detector (0x00-0x1F + DEL 0x7F).
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# Fenced YAML code block (markdown convention). Matches the same shape used
# by ``policy_md.py:_FENCED_YAML_RE``.
_FENCED_YAML_RE = re.compile(
    r"```(?:yaml)?\s*\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)

# Size cap defending against YAML alias-bomb / billion-laughs DoS. Same
# threshold used in ``policy_md.py:MAX_POLICY_MD_BYTES``.
MAX_PERSONA_LINK_MD_BYTES = 256 * 1024  # 256 KiB


# ──────────────────────────────────────────────────────────────────────────────
# Output dataclass


@dataclass(frozen=True)
class PersonaLink:
    """Parsed contents of a ``persona.link.md`` file.

    ``kind`` is the categorical label (currently only ``"shared"``).
    ``persona_id`` is the identifier within the kind, validated against the
    Protocol-wide charset ``[a-zA-Z0-9_.+@-]+``.
    """

    kind: str
    persona_id: str


# ──────────────────────────────────────────────────────────────────────────────
# Public entry points


def parse_persona_link_md(path: Path) -> PersonaLink:
    """Read and parse a ``persona.link.md`` file.

    Args:
        path: Filesystem path to the file. Callers MUST verify the path
            exists before calling (``path.is_file()``); this function does
            not translate ``FileNotFoundError`` into a custom exception.

    Returns:
        The parsed ``PersonaLink``.

    Raises:
        PersonaLinkInvalid: On any structural error (size cap exceeded,
            non-UTF-8 bytes, no YAML block found, malformed YAML, missing
            ``kind:``, unknown ``kind:`` value, missing ``persona_id:``,
            ``persona_id`` charset failure, control characters).
        OSError: On I/O error reading the file (caller's job to translate
            or re-raise).
    """
    try:
        st = path.stat()
    except OSError:
        raise

    if st.st_size > MAX_PERSONA_LINK_MD_BYTES:
        raise PersonaLinkInvalid(
            f"{path} exceeds the {MAX_PERSONA_LINK_MD_BYTES}-byte size cap "
            f"(got {st.st_size}B). The cap defends against YAML alias-bomb "
            f"DoS; a persona.link.md should be a few hundred bytes."
        )

    raw_bytes = path.read_bytes()
    try:
        text = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PersonaLinkInvalid(f"{path} is not valid UTF-8: {exc}") from exc

    return parse_persona_link_md_text(text, source=str(path))


def parse_persona_link_md_text(text: str, *, source: str = "<text>") -> PersonaLink:
    """Parse ``persona.link.md`` content from a text string.

    Used directly by tests and by callers that already hold the file text.
    The companion ``parse_persona_link_md`` reads the file then calls this.

    Args:
        text: The full file contents (including the markdown code fence).
        source: Optional source label for error messages (file path,
            ``"<text>"`` by default).

    Returns:
        The parsed ``PersonaLink``.

    Raises:
        PersonaLinkInvalid: On any structural error.
    """
    yaml_text = _extract_yaml(text, source)
    raw = _load_yaml(yaml_text, source)
    return _build_link(raw, source)


# ──────────────────────────────────────────────────────────────────────────────
# YAML extraction


def _extract_yaml(text: str, source: str) -> str:
    """Extract the YAML content from the markdown code fence.

    Operator-edited files have one YAML block inside a fence; the prose
    around the block is documentation. The first fenced block wins.

    Raises ``PersonaLinkInvalid`` when no fenced block is present.
    """
    match = _FENCED_YAML_RE.search(text)
    if match is None:
        raise PersonaLinkInvalid(
            f"{source}: no YAML code block found. Expected a fenced block "
            f"like:\n\n    ```yaml\n    kind: shared\n    persona_id: ...\n    ```"
        )
    return match.group(1)


def _load_yaml(yaml_text: str, source: str) -> dict[str, Any]:
    """Parse YAML text into a top-level mapping.

    Uses ``yaml.safe_load`` (blocks arbitrary-object construction). Refuses
    non-mapping top-level values (lists, scalars, null) because the contract
    requires the two scalar fields ``kind`` and ``persona_id`` keyed under
    a mapping.
    """
    try:
        obj = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise PersonaLinkInvalid(
            f"{source}: malformed YAML in code block: {exc}"
        ) from exc

    if obj is None:
        raise PersonaLinkInvalid(
            f"{source}: YAML code block is empty. Expected fields "
            f"'kind' and 'persona_id'."
        )
    if not isinstance(obj, dict):
        raise PersonaLinkInvalid(
            f"{source}: YAML top-level must be a mapping (dict); got "
            f"{type(obj).__name__}."
        )
    return obj


# ──────────────────────────────────────────────────────────────────────────────
# Field validation


def _build_link(raw: dict[str, Any], source: str) -> PersonaLink:
    """Validate the parsed mapping into a ``PersonaLink`` record."""
    kind = _require_scalar(raw, "kind", source)
    persona_id = _require_scalar(raw, "persona_id", source)

    if kind not in _SUPPORTED_KINDS:
        supported = ", ".join(sorted(_SUPPORTED_KINDS))
        raise PersonaLinkInvalid(
            f"{source}: kind={kind!r} is not supported. Supported kinds: {supported}."
        )

    _validate_persona_id(persona_id, source)
    return PersonaLink(kind=kind, persona_id=persona_id)


def _require_scalar(raw: dict[str, Any], field_name: str, source: str) -> str:
    """Return ``raw[field_name]`` as a string, refusing missing or wrong-type fields."""
    if field_name not in raw:
        raise PersonaLinkInvalid(f"{source}: missing required field '{field_name}'.")
    value = raw[field_name]
    if not isinstance(value, str):
        raise PersonaLinkInvalid(
            f"{source}: field '{field_name}' must be a string; got "
            f"{type(value).__name__}."
        )
    if not value:
        raise PersonaLinkInvalid(
            f"{source}: field '{field_name}' must be a non-empty string."
        )
    return value


def _validate_persona_id(persona_id: str, source: str) -> None:
    """Validate ``persona_id`` against the Protocol-wide charset.

    Refuses: empty strings, leading dots (hidden-file traversal),
    ``..`` substrings (directory traversal), path separators (``/`` or
    ``\\``), control characters, and anything not matching the charset
    ``[a-zA-Z0-9_.+@-]+``.

    Mirrors ``persona/filesystem.py:_validate_persona_id``. Each parser
    in the framework duplicates this small validation block rather than
    importing across modules (the precedent set by ``policy_md.py``).
    """
    if persona_id.startswith("."):
        raise PersonaLinkInvalid(
            f"{source}: persona_id must not start with '.'; got {persona_id!r}."
        )
    if ".." in persona_id:
        raise PersonaLinkInvalid(
            f"{source}: persona_id must not contain '..'; got {persona_id!r}."
        )
    if "/" in persona_id or "\\" in persona_id:
        raise PersonaLinkInvalid(
            f"{source}: persona_id must not contain path separators; got "
            f"{persona_id!r}."
        )
    if _CONTROL_CHARS.search(persona_id):
        raise PersonaLinkInvalid(
            f"{source}: persona_id must not contain control characters or "
            f"newlines; got {persona_id!r}."
        )
    if not _PERSONA_ID_PATTERN.match(persona_id):
        raise PersonaLinkInvalid(
            f"{source}: persona_id must match [a-zA-Z0-9_.+@-]+; got {persona_id!r}."
        )

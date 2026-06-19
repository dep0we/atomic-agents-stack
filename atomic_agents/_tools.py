"""Parse tools.md to extract read paths, write paths, external APIs, hard NOs.

tools.md is markdown with sections; this is a lightweight parser that finds
the "Read paths", "Write paths", "External APIs", and "Hard NOs" sections
and extracts bullet items as plain strings (paths get expanded).
"""

from __future__ import annotations
import logging
import re
import warnings
from pathlib import Path

from ._platform import expand, resolve_under_agent_root

_logger = logging.getLogger(__name__)


# Match exact phrases (case-insensitive). The section ends at the next "## " header,
# whether or not we recognize it. Loose prefix matching caused bugs with sections
# like "## Read budget" being absorbed into read_paths.
SECTION_PATTERNS = {
    "read_paths": re.compile(r"^##\s+Read paths?\b", re.IGNORECASE),
    "write_paths": re.compile(r"^##\s+Write paths?\b", re.IGNORECASE),
    "read_only_paths": re.compile(r"^##\s+Read[\s-]only paths?\b", re.IGNORECASE),
    "external_apis": re.compile(r"^##\s+External APIs?\b", re.IGNORECASE),
    "hard_nos": re.compile(r"^##\s+Hard NOs?\b", re.IGNORECASE),
}


# Judge-layer per-tool action-class section header (spec/28 + #112 PR 2a).
# Kept separate from SECTION_PATTERNS because the bullet format is
# ``tool_name: action_class`` rather than the path/text shape the other
# sections use — and the resulting parse is a dict, not a list. Used by
# parse_tool_classifications_text() only.
_TOOL_CLASSIFICATION_HEADER = re.compile(
    r"^##\s+Tool classifications?\b", re.IGNORECASE
)


# Valid ActionClass enum values from atomic_agents/judge/types.py.
# Kept as a literal here to keep _tools.py free of judge-module imports
# (would otherwise force tools.py → judge → memory load order at parse
# time). The judge layer asserts parity in its conformance suite.
_VALID_ACTION_CLASSES: frozenset[str] = frozenset(
    {
        "read_only",
        "reversible_write",
        "external_side_effect",
        "high_risk",
    }
)


def parse_tools_md(path: Path, agent_root: Path | None = None) -> dict:
    """Parse tools.md from disk. Thin wrapper around parse_tools_md_text.

    When agent_root is supplied, bare-relative path tokens (not starting with
    / or ~) are resolved under agent_root rather than the process CWD.
    """
    if not path.exists():
        return parse_tools_md_text("", agent_root=agent_root)
    return parse_tools_md_text(path.read_text(encoding="utf-8"), agent_root=agent_root)


def parse_tools_md_text(text: str, agent_root: Path | None = None) -> dict:
    """Parse tools.md content into a dict of section -> list of items.

    Items are stripped bullet-point text. Paths are expanded (~/, etc.).

    When agent_root is supplied, bare-relative path tokens (not starting with
    / or ~) are resolved under agent_root instead of the process CWD. This is
    the correct behavior for tools.md paths: a token like 'memory/' always
    means <agent_folder>/memory/, not <process_cwd>/memory/. Callers that
    have agent_root in scope MUST pass it; callers without it (e.g. DB
    round-trip reconstruction) fall back to CWD-anchor and emit a warning.

    Sections we don't recognize (e.g., "## Read budget", "## Helpers") are
    skipped -- current_section resets when we hit any unrecognized H2.

    Use this when the source is cascade-merged content (role + instance
    override) rather than a single file on disk.
    """
    sections = {key: [] for key in SECTION_PATTERNS}
    if not text:
        return sections

    current_section = None
    for line in text.splitlines():
        stripped = line.rstrip()

        # Detect any H2 header
        if stripped.startswith("## "):
            # Match against known sections
            matched = None
            for key, pattern in SECTION_PATTERNS.items():
                if pattern.match(stripped):
                    matched = key
                    break
            current_section = matched  # None if unrecognized -- exits collection mode
            continue

        # Skip H1 / H3+ headers, blank lines, prose
        if stripped.startswith("#") or current_section is None:
            continue

        # Bullet point
        bullet_match = re.match(r"^\s*[-*]\s+(.+?)$", stripped)
        if not bullet_match:
            continue
        item = bullet_match.group(1).strip()

        if current_section in ("read_paths", "write_paths", "read_only_paths"):
            # Strip backticks and trailing comments
            item = re.sub(r"`", "", item)
            # Take everything up to a comma, paren, or em-dash divider
            item = re.split(r"\s+(?:—|--|\(|,)\s*", item, maxsplit=1)[0].strip()
            if item:
                is_bare_relative = not (
                    item.startswith("~") or Path(item).is_absolute()
                )
                if is_bare_relative and agent_root is not None:
                    resolved = resolve_under_agent_root(item, agent_root)
                    # Debug-level provenance, not an operator warning: bare-
                    # relative tokens are the canonical default-template shape
                    # (spec/01), so anchoring them under agent_root is the
                    # correct happy path — nothing was misconfigured. A WARNING
                    # here would spam stderr on every agent load / doctor run.
                    # Operators who want to trace path resolution enable DEBUG.
                    _logger.debug(
                        "tools.md: anchored bare-relative path %r under agent_root %s",
                        item,
                        agent_root,
                    )
                    sections[current_section].append(resolved)
                elif is_bare_relative and agent_root is None:
                    warnings.warn(
                        f"tools.md: bare-relative path {item!r} resolved "
                        "against process CWD because agent_root was not "
                        "supplied to parse_tools_md_text(). Thread agent_root "
                        "for correct anchor (spec/01 portability).",
                        stacklevel=3,
                    )
                    sections[current_section].append(expand(item))
                else:
                    sections[current_section].append(expand(item))
        else:
            sections[current_section].append(item)

    return sections


def parse_tool_classifications(path: Path) -> dict[str, str]:
    """Parse ``tools.md``'s ``## Tool classification`` section into a
    ``{tool_name: action_class}`` map.

    Per spec/28 + #112 PR 2a: the judge layer needs a per-tool
    ``ActionClass`` to build ``ActionProposal.classification``. Two
    sources can supply the value:

    1. ``ToolDefinition.classification`` (set when the tool is
       registered in code).
    2. This ``## Tool classification`` section in ``tools.md`` (operator
       can override or supply a class for tools whose registration
       didn't set one).

    Bullet shape::

        - tool_name: external_side_effect
        - send_email: external_side_effect — outward-facing, irreversible
        - read_calendar: read_only

    Action-class values must be one of the four ``ActionClass`` enum
    strings (``read_only``, ``reversible_write``,
    ``external_side_effect``, ``high_risk``). Invalid values are
    silently skipped — the framework defaults unmapped tools to
    ``external_side_effect`` per spec/28's safe default. PR 4's
    conformance suite asserts the parser produces the same map as the
    operator wrote, so silent-skip is acceptable here without a logger
    surface (operators learn the mapping via doctor checks, not
    parser warnings).
    """
    if not path.exists():
        return parse_tool_classifications_text("")
    return parse_tool_classifications_text(path.read_text(encoding="utf-8"))


def parse_tool_classifications_text(text: str) -> dict[str, str]:
    """Parse a ``## Tool classification`` section from in-memory text.

    Same behavior as ``parse_tool_classifications`` but for
    cascade-merged content (role + instance override). Returns an
    empty dict when the section is absent.
    """
    result: dict[str, str] = {}
    if not text:
        return result

    in_section = False
    for line in text.splitlines():
        stripped = line.rstrip()
        if stripped.startswith("## "):
            in_section = bool(_TOOL_CLASSIFICATION_HEADER.match(stripped))
            continue
        if not in_section:
            continue
        if stripped.startswith("#") or not stripped.strip():
            continue

        bullet_match = re.match(r"^\s*[-*]\s+(.+?)$", stripped)
        if not bullet_match:
            continue
        item = bullet_match.group(1).strip()
        # ``tool_name: action_class [— optional trailing comment]``
        if ":" not in item:
            continue
        name_part, rest = item.split(":", 1)
        tool_name = name_part.strip().strip("`")
        # Take everything up to a comma, em-dash, or paren divider.
        cls_value = (
            re.split(r"\s+(?:—|--|\(|,)\s*", rest.strip(), maxsplit=1)[0]
            .strip()
            .strip("`")
            # Normalize case so operators writing
            # ``Send_Email: External_Side_Effect`` map cleanly — the
            # silent-skip on capitalized values was operator-hostile
            # per the round-2 review.
            .lower()
        )
        if not tool_name or cls_value not in _VALID_ACTION_CLASSES:
            continue
        result[tool_name] = cls_value
    return result

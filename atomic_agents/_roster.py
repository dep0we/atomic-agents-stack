"""Parse roster.md to extract the list of delegate-able agent names.

roster.md format (mirrors tools.md section style):

    # Roster

    ## Delegate to

    - editor — proofreads drafts, checks style guide adherence
    - director — high-level scope and continuity decisions
    - researcher — fact-checks historical and technical claims

    ## Notes

    Plain prose: when to call which specialist.

Only the "Delegate to" section is parsed for agent names. The agent name is
the first word of each bullet; everything after the first space / em-dash /
hyphen is treated as a human-readable comment and ignored.
"""

from __future__ import annotations
import re
from pathlib import Path


# Match the "## Delegate to" section header (case-insensitive, plural-tolerant)
_DELEGATE_SECTION = re.compile(r"^##\s+Delegate\s+to\b", re.IGNORECASE)
# Bullet: any leading whitespace, dash or star, whitespace, then content
_BULLET = re.compile(r"^\s*[-*]\s+(.+?)$")
# Separators that end the agent name: em-dash, double-dash, space, comma
_NAME_END = re.compile(r"\s+(?:—|--|–|-\s|\(|,).*")


def parse_roster_md(path: Path) -> list[str]:
    """Parse roster.md from disk. Returns list of agent names."""
    if not path.exists():
        return []
    return parse_roster_md_text(path.read_text(encoding="utf-8"))


def parse_roster_md_text(text: str) -> list[str]:
    """Parse roster.md content into a list of agent names.

    Only the "## Delegate to" section is parsed. Lines in other sections
    (## Notes, etc.) are ignored. Blank lines and H1/H3+ headers are skipped.
    The agent name is the first whitespace/dash-separated token on each bullet.
    """
    if not text:
        return []

    names: list[str] = []
    in_delegate_section = False

    for line in text.splitlines():
        stripped = line.rstrip()

        # Detect any H2 header
        if stripped.startswith("## "):
            in_delegate_section = bool(_DELEGATE_SECTION.match(stripped))
            continue

        # Skip H1 / H3+ headers
        if stripped.startswith("#"):
            continue

        if not in_delegate_section:
            continue

        # Bullet point
        bullet_match = _BULLET.match(stripped)
        if not bullet_match:
            continue

        item = bullet_match.group(1).strip()
        # Strip trailing comments after the name
        item = _NAME_END.sub("", item).strip()
        # Take just the first token as the name (safety: no spaces in names)
        name = item.split()[0] if item.split() else ""
        # Strip any trailing punctuation that bled through (e.g. "delta,")
        name = name.rstrip(",;:")
        if name:
            names.append(name)

    return names

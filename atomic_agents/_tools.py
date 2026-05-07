"""Parse tools.md to extract read paths, write paths, external APIs, hard NOs.

tools.md is markdown with sections; this is a lightweight parser that finds
the "Read paths", "Write paths", "External APIs", and "Hard NOs" sections
and extracts bullet items as plain strings (paths get expanded).
"""

from __future__ import annotations
import re
from pathlib import Path

from ._platform import expand


# Match exact phrases (case-insensitive). The section ends at the next "## " header,
# whether or not we recognize it. Loose prefix matching caused bugs with sections
# like "## Read budget" being absorbed into read_paths.
SECTION_PATTERNS = {
    "read_paths": re.compile(r"^##\s+Read paths?\b", re.IGNORECASE),
    "write_paths": re.compile(r"^##\s+Write paths?\b", re.IGNORECASE),
    "external_apis": re.compile(r"^##\s+External APIs?\b", re.IGNORECASE),
    "hard_nos": re.compile(r"^##\s+Hard NOs?\b", re.IGNORECASE),
}


def parse_tools_md(path: Path) -> dict:
    """Parse tools.md into a dict of section -> list of items.

    Items are stripped bullet-point text. Paths are expanded (~/, etc.).
    Sections we don't recognize (e.g., "## Read budget", "## Helpers") are
    skipped — current_section resets when we hit any unrecognized H2.
    """
    if not path.exists():
        return {"read_paths": [], "write_paths": [], "external_apis": [], "hard_nos": []}

    text = path.read_text(encoding="utf-8")
    sections = {key: [] for key in SECTION_PATTERNS}

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
            current_section = matched  # None if unrecognized — exits collection mode
            continue

        # Skip H1 / H3+ headers, blank lines, prose
        if stripped.startswith("#") or current_section is None:
            continue

        # Bullet point
        bullet_match = re.match(r"^\s*[-*]\s+(.+?)$", stripped)
        if not bullet_match:
            continue
        item = bullet_match.group(1).strip()

        if current_section in ("read_paths", "write_paths"):
            # Strip backticks and trailing comments
            item = re.sub(r"`", "", item)
            # Take everything up to a comma, paren, or em-dash divider
            item = re.split(r"\s+(?:—|--|\(|,)\s*", item, maxsplit=1)[0].strip()
            if item:
                sections[current_section].append(expand(item))
        else:
            sections[current_section].append(item)

    return sections

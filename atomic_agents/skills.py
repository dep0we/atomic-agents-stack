"""Skills primitive — filesystem-based reusable expertise modules (spec/18).

A skill is a folder under ``<agent>/skills/<skill-name>/`` with a SKILL.md
entry point (YAML frontmatter + markdown body) and optional supporting files
(one level deep, ≤500 lines per file recommended).

Skills are loaded progressively:
- At init, only metadata (name + description) lands in the system prompt.
- ``load_skill`` and ``load_skill_file`` built-in tools are registered in the
  agent's ToolRegistry; the model invokes them to pull a skill's full body or
  a referenced file into the conversation when relevant.

This mirrors Anthropic's progressive disclosure pattern and avoids the
"every skill costs context tokens upfront" problem.

Usage::

    # Skills are auto-discovered at agent init (no operator code needed).
    # Operator just drops a SKILL.md into <agent>/skills/<skill-name>/:
    #
    #   agents/my-agent/skills/spreadsheet-analysis/SKILL.md
    #
    # The SKILL.md frontmatter must include `name` and `description`.

Naming conventions:
    - Gerund-form preferred: ``spreadsheet-analysis``, ``financial-modeling``
    - lowercase + hyphens + digits only
    - ≤64 chars
    - Reserved words blocked: ``anthropic``, ``claude``, ``atomic_agents``

Description conventions (third person, what + when, ≤1024 chars):
    "Processes Excel and CSV files, generates pivot tables, summarizes tabular
    data. Use when the user mentions spreadsheets, .xlsx files, CSV data, or
    asks for tabular analysis."
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import frontmatter as _frontmatter

from .exceptions import SkillFileTraversal

_logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────
# Constants

SKILL_ENTRY_POINT = "SKILL.md"
BODY_LINE_WARN_THRESHOLD = 500
DESCRIPTION_MAX_CHARS = 1024
NAME_MAX_CHARS = 64
WHEN_TO_USE_MAX_WORDS = 200

# Reserved words that may not appear as (or within) a skill name
_RESERVED_WORDS: frozenset[str] = frozenset({"anthropic", "claude", "atomic_agents"})

# Valid skill name pattern: lowercase letters, digits, and hyphens only
_VALID_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*$")


# ──────────────────────────────────────────────────────────────────
# SkillManifest dataclass


@dataclass
class SkillManifest:
    """Parsed metadata for a single skill.

    Attributes:
        name: Skill identifier (matches the frontmatter ``name`` field).
        description: Third-person description (what it does + when to use it).
        when_to_use: Optional extended triggering guidance (≤200 words).
        skill_dir: Absolute path to the skill's folder.
        skill_md_path: Absolute path to the SKILL.md entry point.
        body_lines: Number of lines in the SKILL.md body (excluding frontmatter).
            Values > 500 generate a warning during discovery.
    """

    name: str
    description: str
    when_to_use: str | None
    skill_dir: Path
    skill_md_path: Path
    body_lines: int


# ──────────────────────────────────────────────────────────────────
# Validation


def _is_valid_name(name: str) -> tuple[bool, str]:
    """Check skill name validity.

    Returns (is_valid, error_message). error_message is empty when valid.
    """
    if not name:
        return False, "name is required"
    if len(name) > NAME_MAX_CHARS:
        return False, f"name exceeds {NAME_MAX_CHARS} chars: {len(name)}"
    if not _VALID_NAME_RE.match(name):
        return False, (
            f"name must match [a-z0-9][a-z0-9\\-]*: {name!r}. "
            "Use lowercase letters, digits, and hyphens only."
        )
    for reserved in _RESERVED_WORDS:
        if reserved in name:
            return False, f"name contains reserved word {reserved!r}: {name!r}"
    return True, ""


def validate_skill_manifest(skill_dir: Path) -> tuple[SkillManifest | None, list[str]]:
    """Parse and validate SKILL.md in ``skill_dir``.

    Returns ``(manifest, warnings)``. Manifest is ``None`` on hard error (missing
    required fields, invalid name). Warnings include soft issues (body > 500 lines,
    deeply nested references, description too long, when_to_use too long).

    This function is called by :func:`discover_skills` and can also be called
    directly for operator-side linting.
    """
    warnings: list[str] = []
    skill_md = skill_dir / SKILL_ENTRY_POINT

    if not skill_md.is_file():
        return None, [f"no SKILL.md in {skill_dir}"]

    try:
        parsed = _frontmatter.load(skill_md)
    except Exception as exc:
        return None, [f"failed to parse {skill_md}: {exc}"]

    meta = parsed.metadata

    # --- Required: name ---
    name = meta.get("name", "")
    if not isinstance(name, str):
        name = str(name)
    name = name.strip()
    valid, err = _is_valid_name(name)
    if not valid:
        return None, [f"skill {skill_dir.name}: invalid name — {err}"]

    # --- Required: description ---
    description = meta.get("description", "")
    if not isinstance(description, str):
        description = str(description)
    description = description.strip()
    if not description:
        return None, [f"skill {name!r}: description is required"]
    if len(description) > DESCRIPTION_MAX_CHARS:
        warnings.append(
            f"skill {name!r}: description exceeds {DESCRIPTION_MAX_CHARS} chars "
            f"({len(description)}). Consider shortening."
        )

    # --- Optional: when_to_use ---
    when_to_use: str | None = meta.get("when_to_use")
    if when_to_use is not None:
        if not isinstance(when_to_use, str):
            when_to_use = str(when_to_use)
        when_to_use = when_to_use.strip() or None
        if when_to_use:
            word_count = len(when_to_use.split())
            if word_count > WHEN_TO_USE_MAX_WORDS:
                warnings.append(
                    f"skill {name!r}: when_to_use exceeds {WHEN_TO_USE_MAX_WORDS} words "
                    f"({word_count}). Keep it concise for reliable LLM routing."
                )

    # --- Body line count ---
    body_text = parsed.content or ""
    body_lines = len(body_text.splitlines())
    if body_lines > BODY_LINE_WARN_THRESHOLD:
        warnings.append(
            f"skill {name!r}: body is {body_lines} lines "
            f"(Anthropic recommends ≤{BODY_LINE_WARN_THRESHOLD}). "
            "Consider splitting into sub-sections or using referenced files."
        )

    # --- Warn on deeply nested references (> 1 level deep) ---
    _check_deep_references(body_text, name, skill_dir, warnings)

    return SkillManifest(
        name=name,
        description=description,
        when_to_use=when_to_use,
        skill_dir=skill_dir,
        skill_md_path=skill_md,
        body_lines=body_lines,
    ), warnings


def _check_deep_references(body: str, skill_name: str, skill_dir: Path, warnings: list[str]) -> None:
    """Scan body for Markdown links/includes that reference nested paths.

    A reference is considered "deeply nested" if it contains a path separator
    beyond one level (e.g. ``sub/dir/file.md`` has two levels).
    One level deep is fine: ``reference.md``, ``examples.md``.
    """
    # Match Markdown links: [text](path) and bare paths in code spans/text
    link_re = re.compile(r'\[(?:[^\]]*)\]\(([^)]+)\)')
    for match in link_re.finditer(body):
        ref_path = match.group(1).strip()
        # Skip URLs
        if ref_path.startswith(("http://", "https://", "mailto:")):
            continue
        # Check depth: one level means no directory separator in the path
        # (or exactly one component after stripping leading ./)
        normalized = ref_path.lstrip("./")
        parts = [p for p in normalized.replace("\\", "/").split("/") if p]
        if len(parts) > 1:
            warnings.append(
                f"skill {skill_name!r}: reference {ref_path!r} is more than one level "
                "deep. Only one-level-deep references are supported by load_skill_file."
            )


# ──────────────────────────────────────────────────────────────────
# Discovery


def discover_skills(agent_root: Path) -> list[SkillManifest]:
    """Scan ``<agent_root>/skills/*/SKILL.md`` and return parsed manifests.

    Skips directories without a SKILL.md. Logs warnings on validation issues
    but does not raise — operators can fix gradually.

    Returns an empty list if ``<agent_root>/skills/`` does not exist.
    """
    skills_dir = agent_root / "skills"
    if not skills_dir.is_dir():
        return []

    manifests: list[SkillManifest] = []
    for skill_subdir in sorted(skills_dir.iterdir()):
        if not skill_subdir.is_dir():
            continue
        skill_md = skill_subdir / SKILL_ENTRY_POINT
        if not skill_md.is_file():
            _logger.debug("skills: skipping %s — no SKILL.md", skill_subdir.name)
            continue
        manifest, warnings = validate_skill_manifest(skill_subdir)
        for w in warnings:
            _logger.warning("skills: %s", w)
        if manifest is None:
            _logger.warning(
                "skills: skipping %s due to validation error: %s",
                skill_subdir.name, warnings[0] if warnings else "unknown"
            )
            continue
        manifests.append(manifest)
        _logger.debug("skills: loaded %r (%d body lines)", manifest.name, manifest.body_lines)

    return manifests


# ──────────────────────────────────────────────────────────────────
# Loading


def load_skill_body(skill_manifest: SkillManifest) -> str:
    """Return the SKILL.md body (without frontmatter) for in-context loading.

    Strips frontmatter (the ``---``-delimited YAML block at the top) and
    returns the remaining markdown body verbatim.
    """
    try:
        parsed = _frontmatter.load(skill_manifest.skill_md_path)
        return parsed.content or ""
    except Exception as exc:
        _logger.warning(
            "skills: failed to load body for %r: %s", skill_manifest.name, exc
        )
        return ""


def load_skill_referenced_file(skill_manifest: SkillManifest, relative_path: str) -> str:
    """Load a file referenced from SKILL.md (one level deep).

    Security guarantees:
    - Refuses ``../`` traversal — raises :exc:`SkillFileTraversal`.
    - Refuses any resolved path outside ``skill_manifest.skill_dir``.

    Only paths one level deep from the skill directory are supported
    (e.g. ``reference.md``, ``examples.md``). Subdirectories are
    blocked by the traversal check.

    Args:
        skill_manifest: The manifest of the skill owning the file.
        relative_path: Path relative to the skill directory (e.g. ``examples.md``).

    Returns:
        The file's text content.

    Raises:
        SkillFileTraversal: If the path attempts directory traversal.
        FileNotFoundError: If the referenced file does not exist.
    """
    # Normalise path separators for cross-platform comparison
    normalised = relative_path.replace("\\", "/")
    parts = [p for p in normalised.split("/") if p]

    # Block explicit traversal markers (.. in any component)
    if ".." in parts:
        raise SkillFileTraversal(
            f"Path traversal detected in skill {skill_manifest.name!r}: "
            f"{relative_path!r} contains '..'"
        )

    # Also block if .. appears anywhere in the raw string (belt-and-suspenders)
    if ".." in relative_path:
        raise SkillFileTraversal(
            f"Path traversal detected in skill {skill_manifest.name!r}: "
            f"{relative_path!r} contains '..'"
        )

    # Enforce one-level-deep limit: relative_path must be a bare filename
    # with no subdirectory component (spec/18 §37).  More than one path
    # component means the caller is referencing a subdirectory, which is not
    # supported and could be used to reference files in hidden subdirs.
    if len(parts) > 1:
        raise SkillFileTraversal(
            f"Skill {skill_manifest.name!r}: referenced file {relative_path!r} "
            "is more than one level deep. Only bare filenames (no subdirectory "
            "components) are supported by load_skill_file."
        )

    target = (skill_manifest.skill_dir / relative_path).resolve()
    skill_dir_resolved = skill_manifest.skill_dir.resolve()

    # Confirm the resolved path is inside the skill directory
    try:
        target.relative_to(skill_dir_resolved)
    except ValueError:
        raise SkillFileTraversal(
            f"Path {relative_path!r} resolves outside skill directory for "
            f"{skill_manifest.name!r}: {target} is not under {skill_dir_resolved}"
        )

    if not target.exists():
        raise FileNotFoundError(
            f"Skill {skill_manifest.name!r}: referenced file not found: {relative_path!r}"
        )

    return target.read_text(encoding="utf-8")

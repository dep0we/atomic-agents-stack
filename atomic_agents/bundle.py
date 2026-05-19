"""Cascade bundle pre-renderer per spec/26.

Skill-mode atomic-agents agents load their cascade by issuing one Read tool
call per cascade file — 18+ round-trips that cost 30-90s wall time before the
first substantive response (issue #231). This module renders the cascade into
a single concatenated file the skill loads in one Read instead of N.

Source files stay canonical (per CLAUDE.md rule #1 — vault is source of truth);
bundles are derived artifacts cached at:

    $ATOMIC_AGENTS_CACHE_DIR/<slug>.md
    (default: ~/.cache/atomic-agents/bundles/<slug>.md)

The bundle is stale iff its mtime is older than any source file's mtime. The
``--if-stale`` flag (used by skill templates) skips regeneration when fresh;
``--refresh`` forces it. No file watcher is required.

The bundle covers spec/04 steps [1]-[10] for single-agent layouts and the
spec/06 three-layer equivalent for cascaded multi-agent projects — i.e.,
everything the agent's :meth:`AtomicAgent.load` would load at startup. Step
[11] (the work item) is per-invocation user input and is never in the bundle.

The bundle is NOT a replacement for INDEX-driven progressive disclosure during
conversation (rule #6) — wiki pages and non-pinned memory notes still load
on demand via the agent's tools.

See ``docs/spec/26-cascade-bundle.md`` for the full contract.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import _cascade
from ._io import atomic_write


SECTION_SEPARATOR = "\n\n═══════════════════════════\n\n"

# Mirror the agent's startup-load constants so the bundle stays in sync
# with what assemble_system_prompt() would actually produce.
PINNED_MAX = 5
RECENT_NOTES_DEFAULT = 5
RECENT_JOURNAL_DEFAULT = 1


@dataclass(frozen=True)
class BundleResult:
    """Outcome of a render call.

    Attributes:
        path: Absolute path to the bundle file on disk.
        regenerated: True if the bundle was (re)written this call; False when
            ``if_stale=True`` and the existing bundle was fresh.
        section_count: Number of markdown sections in the bundle (``-1`` when
            the existing bundle was reused; the count is not reparsed).
        total_bytes: Size of the bundle in bytes.
        source_count: How many distinct source files contribute to the bundle.
    """

    path: Path
    regenerated: bool
    section_count: int
    total_bytes: int
    source_count: int


def default_cache_dir() -> Path:
    """Resolve the bundle cache directory.

    Honors ``ATOMIC_AGENTS_CACHE_DIR`` env var. Defaults to
    ``~/.cache/atomic-agents/bundles``.
    """
    env = os.environ.get("ATOMIC_AGENTS_CACHE_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / ".cache" / "atomic-agents" / "bundles").resolve()


def slug_for(agent_root: Path, agents_root: Path | None = None) -> str:
    """Generate a filesystem-safe slug for the bundle filename.

    When ``agents_root`` is provided AND ``agent_root`` is under it, the slug
    is the relative path with ``/`` replaced by ``-``. Otherwise the last 5
    path components are used so cascaded layouts (system/projects/proj/agents/role)
    still produce a disambiguated slug.
    """
    if agents_root is not None:
        try:
            rel = agent_root.resolve().relative_to(agents_root.resolve())
            return str(rel).replace(os.sep, "-")
        except ValueError:
            pass
    parts = agent_root.resolve().parts
    tail = parts[-5:] if len(parts) >= 5 else parts[-1:]
    return "-".join(tail).lstrip("-")


def render_bundle(
    agent_root: Path,
    *,
    agents_root: Path | None = None,
    cache_dir: Path | None = None,
    extra_files: list[Path] | None = None,
    if_stale: bool = False,
) -> BundleResult:
    """Render the cascade for *agent_root* into a single bundled file.

    Args:
        agent_root: Path to the agent folder (cascaded or flat layout).
        agents_root: Optional agents-root used to compute the slug. When
            provided AND ``agent_root`` is under it, the slug is the relative
            path; otherwise the last 5 path components are used.
        cache_dir: Override the bundle cache directory. Defaults to
            :func:`default_cache_dir`.
        extra_files: Operator-supplied extras to append after the standard
            cascade. Combined with any ``<agent>/bundle.md`` declarations.
        if_stale: When True, skip regeneration if the existing bundle's mtime
            is at least as new as every source file's mtime. The skill-mode
            invocation path passes this flag.

    Returns:
        :class:`BundleResult` describing the outcome.
    """
    if not agent_root.exists():
        raise FileNotFoundError(f"agent_root does not exist: {agent_root}")

    cache_dir = (cache_dir or default_cache_dir()).expanduser()
    cache_dir.mkdir(parents=True, exist_ok=True)
    slug = slug_for(agent_root, agents_root)
    bundle_path = cache_dir / f"{slug}.md"

    all_extras = _collect_extras(agent_root, extra_files)
    sources = _source_paths(agent_root) + all_extras

    if if_stale and bundle_path.is_file():
        bundle_mtime = bundle_path.stat().st_mtime
        max_source = max(
            (p.stat().st_mtime for p in sources if p.is_file()),
            default=0.0,
        )
        if max_source <= bundle_mtime:
            return BundleResult(
                path=bundle_path,
                regenerated=False,
                section_count=-1,
                total_bytes=bundle_path.stat().st_size,
                source_count=sum(1 for p in sources if p.is_file()),
            )

    sections = _render_sections(agent_root, all_extras)
    header = _render_header(agent_root, sources)
    body = SECTION_SEPARATOR.join(s for s in sections if s)
    content = header + "\n\n" + body + "\n\n<!-- end bundle -->\n"

    atomic_write(bundle_path, content)

    return BundleResult(
        path=bundle_path,
        regenerated=True,
        section_count=sum(1 for s in sections if s),
        total_bytes=len(content.encode("utf-8")),
        source_count=sum(1 for p in sources if p.is_file()),
    )


# ──────────────────────────────────────────────────────────────────
# Extras


def _collect_extras(
    agent_root: Path,
    cli_extras: list[Path] | None,
) -> list[Path]:
    """Resolve all operator extras.

    Two sources, both optional:

    - ``<agent>/bundle.md``: declarative file listing one path per line.
      Supports ``- path`` markdown-list shape, ``# comment`` lines, and
      backtick-quoted paths. Globs (``*``, ``?``, ``[]``) are expanded.
      Relative paths resolve under ``agent_root``.
    - ``cli_extras``: ad-hoc paths from ``--extra-file``.

    Raises ``FileNotFoundError`` when a declared path doesn't exist (no
    silent drops — operators should know when their extras are missing).
    """
    extras: list[Path] = []

    bundle_md = agent_root / "bundle.md"
    if bundle_md.is_file():
        for raw in bundle_md.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("- "):
                line = line[2:].strip()
            if line.startswith("`") and line.endswith("`") and len(line) > 1:
                line = line[1:-1]
            if not line:
                continue

            expanded = Path(line).expanduser()
            if any(ch in line for ch in "*?["):
                # Glob support — relative globs resolve under agent_root.
                if expanded.is_absolute():
                    root = expanded.anchor or "/"
                    pattern = str(expanded.relative_to(root))
                    matches = sorted(Path(root).glob(pattern))
                else:
                    matches = sorted(agent_root.glob(str(expanded)))
                file_matches = [m for m in matches if m.is_file()]
                if not file_matches:
                    raise FileNotFoundError(
                        f"bundle.md glob matched no files: {raw!r}"
                    )
                extras.extend(file_matches)
            else:
                target = expanded if expanded.is_absolute() else (agent_root / expanded)
                if not target.is_file():
                    raise FileNotFoundError(
                        f"bundle.md references missing file: {target} (line: {raw!r})"
                    )
                extras.append(target)

    if cli_extras:
        for p in cli_extras:
            resolved = Path(p).expanduser()
            if not resolved.is_file():
                raise FileNotFoundError(
                    f"--extra-file path is not a file: {resolved}"
                )
            extras.append(resolved)

    return extras


# ──────────────────────────────────────────────────────────────────
# Source enumeration (for staleness tracking)


def _source_paths(agent_root: Path) -> list[Path]:
    """Enumerate every cascade source file whose mtime should drive staleness."""
    paths: list[Path] = []
    cascade = _cascade.detect_cascade(agent_root)

    if cascade:
        # Layer 1: role (shared template)
        for name in ("PROMPT.md", "tools.md", "model.md"):
            paths.append(cascade.role_root / name)
        # Layer 3: instance (project x role)
        for name in ("tools.override.md", "tools.md", "model.md"):
            paths.append(cascade.instance_root / name)
        for name in ("IDENTITY.md", "SOUL.md", "USER.md"):
            paths.append(cascade.instance_root / "persona" / name)
        # Layer 2: project (shared world)
        for name in ("canon.md", "style_guide.md", "goal.md"):
            paths.append(cascade.project_root / name)
        policy_dir = cascade.project_root / "policy"
        if policy_dir.is_dir():
            paths.extend(sorted(policy_dir.rglob("*.md")))
        instance_root = cascade.instance_root
    else:
        for name in ("IDENTITY.md", "SOUL.md", "USER.md"):
            paths.append(agent_root / "persona" / name)
        for name in ("goal.md", "tools.md", "model.md"):
            paths.append(agent_root / name)
        instance_root = agent_root

    memory_dir = instance_root / "memory"
    wiki_dir = instance_root / "wiki"
    journal_dir = instance_root / "journal"

    if memory_dir.is_dir():
        paths.append(memory_dir / "INDEX.md")
        # All notes feed the pinned/recent selection — track all .md.
        paths.extend(sorted(memory_dir.glob("*.md")))
    if wiki_dir.is_dir():
        paths.append(wiki_dir / "INDEX.md")
    if journal_dir.is_dir():
        # Newest first; only the top N drive staleness since older entries
        # aren't in the bundle. Sort here matches _load_recent_journal.
        paths.extend(
            sorted(journal_dir.rglob("*.md"), reverse=True)[:RECENT_JOURNAL_DEFAULT]
        )

    # bundle.md itself: if it changes, the bundle is stale.
    bundle_md = agent_root / "bundle.md"
    if bundle_md.is_file():
        paths.append(bundle_md)

    return [p for p in paths if p.is_file()]


# ──────────────────────────────────────────────────────────────────
# Section rendering


def _render_sections(agent_root: Path, extras: list[Path]) -> list[str]:
    """Build the ordered list of bundle sections per spec/04 + spec/06.

    Section headers mirror spec/04 §"Cache breakpoints" so a future caller
    that wants to map sections back to Anthropic prompt-cache breakpoints
    can parse them.
    """
    cascade = _cascade.detect_cascade(agent_root)
    sections: list[str] = ["# === BREAKPOINT 1: Stable cascade ==="]

    if cascade:
        sections.extend(_render_cascaded(cascade))
    else:
        sections.extend(_render_flat(agent_root))

    if extras:
        sections.append("# === BREAKPOINT 1.5: Operator extras ===")
        for p in extras:
            sections.append(_render_file_section(p, label=f"Extra · {p.name}"))

    instance_root = cascade.instance_root if cascade else agent_root
    sections.extend(_render_memory_breakpoint(instance_root))
    sections.extend(_render_recent_notes_breakpoint(instance_root))
    sections.extend(_render_journal_breakpoint(instance_root))

    return sections


def _render_cascaded(cascade: _cascade.CascadePaths) -> list[str]:
    """Render BP1 content for a cascaded layout per spec/06."""
    out: list[str] = []

    role_prompt = _cascade.load_role_prompt(cascade)
    if role_prompt:
        out.append(
            f"## Role layer · PROMPT.md\n`{cascade.role_root / 'PROMPT.md'}`\n\n{role_prompt}"
        )

    # Instance persona — all three files per spec/06 Layer 3.
    for persona_name in ("IDENTITY.md", "SOUL.md", "USER.md"):
        p = cascade.instance_root / "persona" / persona_name
        if p.is_file():
            out.append(
                _render_file_section(p, label=f"Instance persona · {persona_name}")
            )

    # Tools — merged via _cascade.resolve_tools_md (instance override appended
    # to role base, or instance full-replacement, or role base).
    tools_source, tools_text = _cascade.resolve_tools_md(cascade)
    if tools_text.strip():
        label = "Tools (merged)" if tools_source else "Tools"
        out.append(
            f"## {label}\n`{tools_source or '(none)'}`\n\n{tools_text.strip()}"
        )

    model_source = _cascade.resolve_model_md(cascade)
    if model_source and model_source.is_file():
        out.append(_render_file_section(model_source, label="Model"))

    # Project shared layer per spec/06 Layer 2.
    project = _cascade.load_project_layer(cascade)
    if project["canon"]:
        out.append(
            f"## Project shared · canon.md\n`{cascade.project_root / 'canon.md'}`\n\n{project['canon']}"
        )
    if project["style_guide"]:
        out.append(
            f"## Project shared · style_guide.md\n`{cascade.project_root / 'style_guide.md'}`\n\n{project['style_guide']}"
        )
    if project["goal"]:
        out.append(
            f"## Project shared · goal.md\n`{cascade.project_root / 'goal.md'}`\n\n{project['goal']}"
        )
    if project["policy"]:
        out.append(
            f"## Project shared · policy/\n`{cascade.project_root / 'policy/'}`\n\n{project['policy']}"
        )

    return out


def _render_flat(agent_root: Path) -> list[str]:
    """Render BP1 content for a single-agent (non-cascaded) layout per spec/04."""
    out: list[str] = []

    for persona_name in ("IDENTITY.md", "SOUL.md", "USER.md"):
        p = agent_root / "persona" / persona_name
        if p.is_file():
            out.append(_render_file_section(p, label=f"Persona · {persona_name}"))

    for name in ("goal.md", "tools.md", "model.md"):
        p = agent_root / name
        if p.is_file():
            out.append(_render_file_section(p, label=name))

    return out


def _render_memory_breakpoint(instance_root: Path) -> list[str]:
    """Render memory INDEX + pinned + wiki INDEX (BP1 trailing or BP2)."""
    memory_dir = instance_root / "memory"
    wiki_dir = instance_root / "wiki"

    out: list[str] = []
    if (memory_dir / "INDEX.md").is_file():
        out.append(_render_file_section(memory_dir / "INDEX.md", label="Memory · INDEX.md"))

    pinned = _load_pinned_notes(memory_dir)
    if pinned:
        out.append(
            "## Memory · Pinned atomic notes\n\n" + "\n\n---\n\n".join(pinned)
        )

    if (wiki_dir / "INDEX.md").is_file():
        out.append(_render_file_section(wiki_dir / "INDEX.md", label="Wiki · INDEX.md"))

    if out:
        return ["# === BREAKPOINT 2: Weekly (INDEXes + pinned) ==="] + out
    return []


def _render_recent_notes_breakpoint(instance_root: Path) -> list[str]:
    """Render the recent atomic notes (BP3) excluding pinned, newest first."""
    memory_dir = instance_root / "memory"
    if not memory_dir.is_dir():
        return []

    pinned_names = {p.name for p in _iter_pinned(memory_dir)}
    candidates = [
        p for p in memory_dir.glob("*.md")
        if p.name not in {"INDEX.md", "bundle.md"} and p.name not in pinned_names
    ]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    recent = candidates[:RECENT_NOTES_DEFAULT]

    if not recent:
        return []

    rendered = [
        f"# {p.name}\n\n{p.read_text(encoding='utf-8')}"
        for p in recent
    ]
    return [
        "# === BREAKPOINT 3: Session (recent atomic notes) ===",
        "## Recent atomic notes\n\n" + "\n\n---\n\n".join(rendered),
    ]


def _render_journal_breakpoint(instance_root: Path) -> list[str]:
    """Render the recent journal entries (BP4), newest first."""
    journal_dir = instance_root / "journal"
    entries = _load_recent_journal(journal_dir, n=RECENT_JOURNAL_DEFAULT)
    if not entries:
        return []
    return [
        "# === BREAKPOINT 4: Daily (recent journal) ===",
        "## Recent journal\n\n" + "\n\n---\n\n".join(entries),
    ]


# ──────────────────────────────────────────────────────────────────
# Memory helpers


def _iter_pinned(memory_dir: Path):
    """Yield Paths to memory notes whose frontmatter contains ``pinned: true``.

    Cheap lexical check (no YAML parser) — matches the agent's own approach
    when scanning memory directories for the pinned filter.
    """
    if not memory_dir.is_dir():
        return
    for path in sorted(memory_dir.glob("*.md")):
        if path.name == "INDEX.md":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if not text.startswith("---"):
            continue
        end = text.find("\n---", 3)
        if end < 0:
            continue
        front = text[3:end]
        for line in front.splitlines():
            stripped = line.strip()
            if stripped.startswith("pinned:"):
                value = stripped.split(":", 1)[1].strip().lower()
                if value in ("true", "yes", "1"):
                    yield path
                break


def _load_pinned_notes(memory_dir: Path, max_pinned: int = PINNED_MAX) -> list[str]:
    """Return rendered pinned atomic notes (full body, including frontmatter)."""
    notes: list[str] = []
    for path in _iter_pinned(memory_dir):
        text = path.read_text(encoding="utf-8")
        notes.append(f"# {path.name}\n\n{text}")
        if len(notes) >= max_pinned:
            break
    return notes


def _load_recent_journal(journal_dir: Path, n: int = RECENT_JOURNAL_DEFAULT) -> list[str]:
    """Return the n most-recent journal entries by filename (newest first).

    Matches :meth:`AtomicAgent._load_recent_journal`: sort by filename
    descending (works because journal entries are dated ``YYYY-MM-DD.md``).
    """
    if not journal_dir.is_dir():
        return []
    entries = sorted(journal_dir.rglob("*.md"), reverse=True)[:n]
    return [
        f"# Journal — {p.stem}\n`{p}`\n\n{p.read_text(encoding='utf-8')}"
        for p in entries
    ]


# ──────────────────────────────────────────────────────────────────
# Rendering helpers


def _render_file_section(path: Path, *, label: str) -> str:
    """Render one file as ``## {label}\\n`{path}`\\n\\n{body}``."""
    return f"## {label}\n`{path}`\n\n{path.read_text(encoding='utf-8').strip()}"


def _render_header(agent_root: Path, sources: list[Path]) -> str:
    """Build the bundle's leading comment header.

    Records timestamp + source mtimes for debuggability. Sources beyond the
    first 25 are summarized to keep the header from dominating the bundle.
    """
    now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    lines = [
        "<!-- atomic-agents cascade bundle (spec/26) -->",
        f"<!-- agent: {agent_root} -->",
        f"<!-- generated: {now} -->",
        f"<!-- sources: {len(sources)} files -->",
    ]
    for p in sources[:25]:
        try:
            mtime = datetime.fromtimestamp(
                p.stat().st_mtime, tz=timezone.utc
            ).isoformat(timespec="seconds")
            try:
                rel = p.relative_to(agent_root)
                rel_str = str(rel)
            except ValueError:
                rel_str = str(p)
            lines.append(f"<!--   {mtime}  {rel_str} -->")
        except OSError:
            continue
    if len(sources) > 25:
        lines.append(f"<!--   ... and {len(sources) - 25} more sources -->")
    return "\n".join(lines)

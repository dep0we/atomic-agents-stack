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
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from . import _cascade
from ._io import atomic_write

if TYPE_CHECKING:
    # Imported under TYPE_CHECKING to avoid circular imports -- bundle.py is
    # loaded early and corpus may not yet be available in all import paths.
    # All runtime references use the string annotation "CorpusBackend | None".
    # (PR 3 wiring)
    from .corpus.backend import CorpusBackend
    from .journal.backend import JournalBackend


SECTION_SEPARATOR = "\n\n═══════════════════════════\n\n"

# Mirror the agent's startup-load constants so the bundle stays in sync
# with what assemble_system_prompt() would actually produce.
PINNED_MAX = 5
RECENT_NOTES_DEFAULT = 5
RECENT_JOURNAL_DEFAULT = 1


# ──────────────────────────────────────────────────────────────────
# Bundle validation (spec/26 `--validate`, issue #593)
#
# The runtime system prompt (AtomicAgent.assemble_system_prompt) and the
# rendered bundle are two independent code paths. `--validate` confirms the
# bundle faithfully CONTAINS every cascade body the runtime injects, catching
# drift between them. This is *content parity*, NOT byte equality: the bundle
# wraps content in scaffolding (HTML comments, BREAKPOINT headers, source-path
# lines, `═══` separators) and legitimately differs in two known ways:
#
#   1. The runtime's `# Available skills` section is absent from the bundle
#      (a known gap tracked as issue #593). Reported, never a failure.
#   2. model.md content is present in the bundle but not in the runtime prompt.
#      (Informational; the bundle is a superset on this axis, so no runtime
#      body goes missing — nothing to validate against the bundle here.)
#
# The agent construction (AtomicAgent + .load() + .assemble_system_prompt())
# stays in the CLI layer so bundle.py never imports the heavy `agent` module.
# This helper is pure text: given the runtime system prompt + the bundle text,
# it classifies each runtime section.

# Header text of the runtime "skills" section emitted by
# AtomicAgent.assemble_system_prompt(). Matched as a section header to classify
# it as a KNOWN divergence rather than unexpected drift.
SKILLS_SECTION_HEADER = "# Available skills"
SKILLS_KNOWN_DIVERGENCE_ISSUE = "#593"

# The runtime synthesizes a placeholder INDEX body even when the agent has no
# memory/wiki notes (MemoryBackend.render_index_summary() returns
# "# Memory Index\n\n" with no entries). The bundle reads INDEX.md from disk
# and omits the section entirely when no file exists. So an EMPTY synthesized
# index is a benign divergence (no real content is lost) — classified as known.
# Whitespace-normalized placeholder bodies (no internal whitespace runs, so the
# literals are already in normalized form — see _normalize_ws).
_EMPTY_INDEX_PLACEHOLDERS = frozenset({"# Memory Index", "# Wiki Index"})
# Runtime section headers whose body, when it is the empty placeholder above,
# is a known divergence rather than drift.
_INDEX_SECTION_HEADERS = frozenset({"# memory/INDEX.md", "# wiki/INDEX.md"})


@dataclass(frozen=True)
class SectionParity:
    """Classification of one runtime system-prompt section against the bundle.

    Attributes:
        header: The section's first line (e.g. ``# project canon.md``).
        status: One of ``"present"`` (body found in the bundle),
            ``"missing"`` (unexpected drift — body NOT found), or
            ``"known"`` (a documented divergence; reported, not a failure).
        note: Human-readable explanation for ``"known"`` / ``"missing"``.
    """

    header: str
    status: str  # "present" | "missing" | "known"
    note: str = ""


@dataclass(frozen=True)
class ValidationReport:
    """Result of comparing the runtime system prompt against a bundle.

    Attributes:
        sections: Per-section parity classifications, in runtime order.
        ok: True iff there is no unexpected drift (no ``"missing"`` section).
            Known divergences do NOT flip this to False.
    """

    sections: list[SectionParity] = field(default_factory=list)

    @property
    def missing(self) -> list[SectionParity]:
        return [s for s in self.sections if s.status == "missing"]

    @property
    def known(self) -> list[SectionParity]:
        return [s for s in self.sections if s.status == "known"]

    @property
    def present(self) -> list[SectionParity]:
        return [s for s in self.sections if s.status == "present"]

    @property
    def ok(self) -> bool:
        return not self.missing


_WS_RE = re.compile(r"\s+")


def _normalize_ws(text: str) -> str:
    """Collapse all runs of whitespace to single spaces and strip ends.

    The bundle reflows cascade bodies through scaffolding (extra blank lines,
    indentation under section headers), so a substring check must compare on
    whitespace-insensitive text. This normalizes BOTH operands the same way.
    """
    return _WS_RE.sub(" ", text).strip()


def split_runtime_sections(system_prompt: str) -> list[tuple[str, str]]:
    """Split an assembled system prompt into ``(header, body)`` sections.

    ``AtomicAgent.assemble_system_prompt()`` joins sections with
    :data:`SECTION_SEPARATOR`. Each section's first line is its header (an
    ``# ``-headed line, e.g. ``# project canon.md``); the remainder is the
    body. Empty sections are dropped. Robust to a missing trailing body.
    """
    sections: list[tuple[str, str]] = []
    for chunk in system_prompt.split(SECTION_SEPARATOR):
        chunk = chunk.strip()
        if not chunk:
            continue
        header, _, body = chunk.partition("\n")
        sections.append((header.strip(), body.strip()))
    return sections


def validate_bundle_parity(
    system_prompt: str,
    bundle_text: str,
) -> ValidationReport:
    """Check every runtime cascade body is present in *bundle_text*.

    Content parity (substring after whitespace normalization), NOT byte
    equality — see the module-level note. The ``# Available skills`` section
    is classified as a KNOWN divergence (issue #593), never as drift.

    Args:
        system_prompt: Output of ``AtomicAgent.assemble_system_prompt()``.
        bundle_text: The rendered bundle's full text.

    Returns:
        A :class:`ValidationReport`. ``report.ok`` is False iff some runtime
        section body is absent from the bundle (unexpected drift).
    """
    bundle_norm = _normalize_ws(bundle_text)
    sections: list[SectionParity] = []

    for header, body in split_runtime_sections(system_prompt):
        # Known divergence: the runtime injects a skills metadata section the
        # bundle deliberately omits (issue #593). Report it, do not fail.
        if header == SKILLS_SECTION_HEADER:
            sections.append(
                SectionParity(
                    header=header,
                    status="known",
                    note=(
                        f"runtime '{SKILLS_SECTION_HEADER}' section is omitted "
                        f"from the bundle (known gap, issue "
                        f"{SKILLS_KNOWN_DIVERGENCE_ISSUE})"
                    ),
                )
            )
            continue

        body_norm = _normalize_ws(body)
        if not body_norm:
            # Header-only section (no body to find) — treat as present; there
            # is nothing whose absence would constitute drift.
            sections.append(SectionParity(header=header, status="present"))
            continue

        # Known divergence: the runtime synthesizes a placeholder INDEX body
        # ("# Memory Index") even with no notes; the bundle omits the section
        # when no INDEX.md file exists. Only the EMPTY placeholder is benign —
        # a populated index that's missing from the bundle is still real drift.
        if header in _INDEX_SECTION_HEADERS and body_norm in _EMPTY_INDEX_PLACEHOLDERS:
            sections.append(
                SectionParity(
                    header=header,
                    status="known",
                    note=(
                        "runtime synthesizes an empty placeholder index "
                        "(no notes); bundle omits the section when no INDEX.md "
                        "file exists — no content is lost"
                    ),
                )
            )
            continue

        if _body_present(body, body_norm, bundle_norm):
            sections.append(SectionParity(header=header, status="present"))
        else:
            sections.append(
                SectionParity(
                    header=header,
                    status="missing",
                    note="runtime body not found in bundle",
                )
            )

    return ValidationReport(sections=sections)


def _body_present(body: str, body_norm: str, bundle_norm: str) -> bool:
    """Return True if *body* is contained in the (normalized) bundle.

    First tries the whole normalized body as a substring. If that misses, the
    runtime section may be a CONCATENATION of multiple files under per-file
    wrapper headers (the persona section joins
    ``# IDENTITY.md\\n\\n<body>`` + ``# SOUL.md\\n\\n<body>`` + ...). The bundle
    renders each file as its own section WITHOUT the runtime's ``# FILENAME.md``
    wrapper line, so the concatenation is never a contiguous substring. Fall
    back to checking each ``# ``-headed sub-block's content (after its own
    header line) individually — every constituent body must be present.
    """
    if body_norm in bundle_norm:
        return True

    sub_blocks = _split_leaf_blocks(body)
    if len(sub_blocks) <= 1:
        # Not a multi-block body — the whole-body miss is real.
        return False

    for block in sub_blocks:
        # Drop the block's own wrapper header line (e.g. "# SOUL.md"); the
        # bundle carries the file CONTENT, not the runtime wrapper.
        _hdr, _, block_body = block.partition("\n")
        block_norm = _normalize_ws(block_body)
        if not block_norm:
            continue
        if block_norm not in bundle_norm:
            return False
    return True


def _split_leaf_blocks(body: str) -> list[str]:
    """Split a section body into ``# ``-headed sub-blocks.

    Splits at line boundaries that begin a new top-level (``# ``) heading, so a
    concatenated persona body becomes one block per file. Lines that are not
    ``# ``-headings stay attached to the preceding block.
    """
    blocks: list[str] = []
    current: list[str] = []
    for line in body.splitlines():
        if line.startswith("# ") and current:
            blocks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def format_validation_report(report: ValidationReport) -> str:
    """Render a :class:`ValidationReport` as a human-readable report.

    PASS form (no unexpected drift)::

        Bundle validation: PASS — content parity holds.
          N runtime sections checked, M present in bundle.
          K known divergence(s) (reported, not failures):
            - # Available skills: ... (issue #593)

    FAILURE form (drift detected)::

        Bundle validation: FAIL — N runtime section(s) missing from bundle.
          Missing content (runtime assembles it; bundle lacks it):
            - # project canon.md: runtime body not found in bundle
    """
    lines: list[str] = []
    checked = len(report.sections)
    if report.ok:
        lines.append("Bundle validation: PASS — content parity holds.")
        lines.append(
            f"  {checked} runtime section(s) checked, "
            f"{len(report.present)} present in bundle."
        )
        if report.known:
            lines.append(
                f"  {len(report.known)} known divergence(s) (reported, not failures):"
            )
            for s in report.known:
                lines.append(f"    - {s.header}: {s.note}")
    else:
        lines.append(
            f"Bundle validation: FAIL — "
            f"{len(report.missing)} runtime section(s) missing from bundle."
        )
        lines.append("  Missing content (runtime assembles it; bundle lacks it):")
        for s in report.missing:
            lines.append(f"    - {s.header}: {s.note}")
        if report.known:
            lines.append("  Known divergence(s) (reported, not the cause of failure):")
            for s in report.known:
                lines.append(f"    - {s.header}: {s.note}")
    return "\n".join(lines)


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
    corpus_backend: "CorpusBackend | None" = None,
    journal_backend: "JournalBackend | None" = None,
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
    sources = _source_paths(agent_root, journal_backend=journal_backend) + all_extras
    staleness_paths = (
        _staleness_paths(agent_root, journal_backend=journal_backend) + all_extras
    )

    if if_stale and bundle_path.is_file():
        bundle_mtime = bundle_path.stat().st_mtime
        # Include directory mtimes alongside file mtimes: POSIX bumps a
        # directory's mtime when files are added or removed in it, which is
        # how we catch the case where a memory note or journal entry was
        # *deleted* (no file mtime would otherwise be newer than the bundle).
        source_mtimes = [p.stat().st_mtime for p in staleness_paths if p.exists()]
        if not source_mtimes:
            # No sources currently exist — either everything was deleted or the
            # cascade is empty. Force regeneration so the bundle reflects reality
            # rather than silently serving stale phantom content.
            pass
        else:
            max_source = max(source_mtimes)
            # Strict < (not <=) catches the same-second edit-and-regenerate case
            # on filesystems with 1s mtime granularity (ext4, HFS+). Equality
            # means "could have been edited just after bundle wrote" — be safe.
            if max_source < bundle_mtime:
                return BundleResult(
                    path=bundle_path,
                    regenerated=False,
                    section_count=-1,
                    total_bytes=bundle_path.stat().st_size,
                    source_count=sum(1 for p in sources if p.is_file()),
                )

    sections = _render_sections(
        agent_root,
        all_extras,
        corpus_backend=corpus_backend,
        journal_backend=journal_backend,
    )
    header = _render_header(agent_root, sources)
    body = SECTION_SEPARATOR.join(s for s in sections if s)
    content = header + "\n\n" + body + "\n\n<!-- end bundle -->\n"

    atomic_write(bundle_path, content)
    # Bundle content can include operator-extras (e.g., ~/.ssh-adjacent
    # identity files, secrets-adjacent operator notes per spec/26 §"Trust
    # model"). Tighten mode so the cache file is owner-readable only —
    # `atomic_write` writes with the inheriting umask, which is often 0644
    # on shared hosts. Defense-in-depth alongside the operator-discipline
    # WritePolicy boundary (round-2 R2-F9).
    try:
        os.chmod(bundle_path, 0o600)
    except OSError:
        # Filesystem doesn't support chmod (FAT32, network mounts in some
        # configurations) — fall through. The bundle is still functional;
        # operator just doesn't get the tightened mode.
        pass

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
                    raise FileNotFoundError(f"bundle.md glob matched no files: {raw!r}")
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
                raise FileNotFoundError(f"--extra-file path is not a file: {resolved}")
            extras.append(resolved)

    return extras


# ──────────────────────────────────────────────────────────────────
# Source enumeration (for staleness tracking)


# TODO(v1.1): _source_paths returns filesystem paths for staleness tracking. SQLite backends have no equivalent path to track. See #65 PR 4 follow-up issue (to be filed at arc closer).
def _source_paths(
    agent_root: Path,
    journal_backend: "JournalBackend | None" = None,
) -> list[Path]:
    """Enumerate every cascade source file whose mtime should drive staleness.

    ADOPT-NOW (#427 PR1 — spec/43): journal entry paths come from
    journal_backend.list_entries(limit=N, newest_first=True) when provided,
    replacing the legacy rglob block. The selection order is byte-identical
    to sorted(journal_dir.rglob('*.md'), reverse=True)[:N] because
    FilesystemJournalBackend.list_entries() sorts by full Path descending.
    When journal_backend is None, get_default_journal_backend(instance_root)
    is called to match the corpus_backend=None pattern.
    """
    from .journal import get_default_journal_backend  # noqa: PLC0415

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

    if memory_dir.is_dir():
        paths.append(memory_dir / "INDEX.md")
        # All notes feed the pinned/recent selection — track all .md.
        paths.extend(sorted(memory_dir.glob("*.md")))
    if wiki_dir.is_dir():
        paths.append(wiki_dir / "INDEX.md")

    # Journal entries via JournalBackend (ADOPT-NOW, #427 PR1).
    # Newest first; only the top N drive staleness since older entries
    # aren't in the bundle. The backend's list_entries() sorts by full Path
    # descending — byte-identical to the legacy sorted(rglob, reverse=True)[:N].
    _jbe = journal_backend or get_default_journal_backend(instance_root)
    journal_entries = _jbe.list_entries(limit=RECENT_JOURNAL_DEFAULT, newest_first=True)
    paths.extend(entry.path for entry in journal_entries)

    # bundle.md itself: if it changes, the bundle is stale.
    bundle_md = agent_root / "bundle.md"
    if bundle_md.is_file():
        paths.append(bundle_md)

    return [p for p in paths if p.is_file()]


def _staleness_paths(
    agent_root: Path,
    journal_backend: "JournalBackend | None" = None,
) -> list[Path]:
    """Return paths whose mtime drives staleness — files PLUS directories.

    Directories are included so that *deletions* trigger regeneration. POSIX
    bumps a directory's mtime when its children are added/removed; without
    this signal, deleting `memory/foo.md` would leave a bundle still
    referencing that note with no source-file mtime newer than the bundle.

    Returns the same files :func:`_source_paths` does plus the parent
    directories that scope the bundle's runtime-assembly content (memory/,
    wiki/, journal/, project policy/).
    """
    paths = _source_paths(agent_root, journal_backend=journal_backend)

    cascade = _cascade.detect_cascade(agent_root)
    instance_root = cascade.instance_root if cascade else agent_root

    # Directories whose mtime tracks add/delete of their direct children.
    dir_candidates = [
        instance_root / "memory",
        instance_root / "wiki",
        instance_root / "journal",
    ]
    if cascade:
        dir_candidates.append(cascade.project_root / "policy")

    paths.extend(d for d in dir_candidates if d.is_dir())
    return paths


# ──────────────────────────────────────────────────────────────────
# Section rendering


def _render_sections(
    agent_root: Path,
    extras: list[Path],
    corpus_backend: "CorpusBackend | None" = None,
    journal_backend: "JournalBackend | None" = None,
) -> list[str]:
    """Build the ordered list of bundle sections per spec/04 + spec/06.

    Section headers mirror spec/04 §"Cache breakpoints" so a future caller
    that wants to map sections back to Anthropic prompt-cache breakpoints
    can parse them.

    ``corpus_backend`` is threaded to ``_render_memory_breakpoint`` so PR 3
    wiring can route wiki INDEX reads through the Protocol when available.
    Defaults to ``None`` for full backward compatibility.

    ``journal_backend`` is threaded to ``_render_journal_breakpoint`` (ADOPT-NOW,
    #427 PR1 — spec/43). Defaults to ``None`` (factory resolved inside).
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
    sections.extend(
        _render_memory_breakpoint(instance_root, corpus_backend=corpus_backend)
    )
    sections.extend(_render_recent_notes_breakpoint(instance_root))
    sections.extend(
        _render_journal_breakpoint(instance_root, journal_backend=journal_backend)
    )

    return sections


def _render_cascaded(cascade: _cascade.CascadePaths) -> list[str]:
    """Render BP1 content for a cascaded layout per spec/06."""
    out: list[str] = []

    # Role PROMPT.md — `_cascade.load_role_prompt` reads with strict UTF-8 and
    # may raise OSError on permission failures. Catch BOTH and fall back to
    # safe-read so a single bad source doesn't crash the whole bundle (F-8 +
    # round-2 R2-F2 from /ship adversarial review).
    role_prompt_path = cascade.role_root / "PROMPT.md"
    try:
        role_prompt = _cascade.load_role_prompt(cascade)
    except (UnicodeDecodeError, OSError):
        role_prompt = (
            _safe_read_text(role_prompt_path).strip()
            if role_prompt_path.is_file()
            else ""
        )
    if role_prompt:
        out.append(f"## Role layer · PROMPT.md\n`{role_prompt_path}`\n\n{role_prompt}")

    # Instance persona — all three files per spec/06 Layer 3.
    for persona_name in ("IDENTITY.md", "SOUL.md", "USER.md"):
        p = cascade.instance_root / "persona" / persona_name
        if p.is_file():
            out.append(
                _render_file_section(p, label=f"Instance persona · {persona_name}")
            )

    # Tools — merged via _cascade.resolve_tools_md (instance override appended
    # to role base, or instance full-replacement, or role base). Catch both
    # UnicodeDecodeError and OSError for the same reason as role PROMPT above.
    try:
        tools_source, tools_text = _cascade.resolve_tools_md(cascade)
    except (UnicodeDecodeError, OSError):
        # Fall back: try the role tools.md via safe-read; mark source as None
        # so the section header reflects that resolution didn't complete.
        role_tools = cascade.role_root / "tools.md"
        tools_source = role_tools if role_tools.is_file() else None
        tools_text = _safe_read_text(role_tools) if role_tools.is_file() else ""
    if tools_text.strip():
        label = "Tools (merged)" if tools_source else "Tools"
        out.append(f"## {label}\n`{tools_source or '(none)'}`\n\n{tools_text.strip()}")

    model_source = _cascade.resolve_model_md(cascade)
    if model_source and model_source.is_file():
        out.append(_render_file_section(model_source, label="Model"))

    # Project shared layer per spec/06 Layer 2. Wrap each project file
    # individually via safe-read so one bad canon.md doesn't lose
    # style_guide / goal / policy sections too.
    project = _safe_load_project_layer(cascade)
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


def _safe_load_project_layer(cascade: _cascade.CascadePaths) -> dict[str, str]:
    """Encoding-tolerant version of :func:`_cascade.load_project_layer`.

    Reads each project file individually via :func:`_safe_read_text` so a
    single non-UTF-8 byte in (say) canon.md doesn't lose style_guide / goal /
    policy too. Returns the same dict shape as the cascade helper.
    """
    out: dict[str, str] = {}
    for name in ("canon", "style_guide", "goal"):
        p = cascade.project_root / f"{name}.md"
        out[name] = _safe_read_text(p).strip() if p.is_file() else ""
    policy_dir = cascade.project_root / "policy"
    if policy_dir.is_dir():
        parts: list[str] = []
        for path in sorted(policy_dir.rglob("*.md")):
            rel = path.relative_to(policy_dir)
            parts.append(
                f"# policy/{rel.as_posix()}\n\n{_safe_read_text(path).strip()}"
            )
        out["policy"] = "\n\n---\n\n".join(parts)
    else:
        out["policy"] = ""
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


def _render_wiki_index_section(label: str, path: Path, content: str) -> str:
    """Render wiki INDEX content into the standard bundle section format.

    Both the corpus_backend Protocol path and the legacy direct-read path
    call this helper so the output is byte-for-byte identical regardless of
    which path produced the content. Matches ``_render_file_section``'s
    ``## {label}\\n`{path}`\\n\\n{body}`` shape exactly. The ``path`` is
    always derivable from ``instance_root`` (``instance_root / "wiki" /
    "INDEX.md"``) regardless of whether the content arrived through the
    Protocol or a direct file read. (PR 3 wiring; IRON RULE assertion 4)
    """
    return f"## {label}\n`{path}`\n\n{content}"


def _render_memory_breakpoint(
    instance_root: Path,
    corpus_backend: "CorpusBackend | None" = None,
) -> list[str]:
    """Render memory INDEX + pinned + wiki INDEX (BP1 trailing or BP2).

    ``corpus_backend`` threads the CorpusBackend Protocol for wiki INDEX
    reads when available (PR 3 wiring). When ``None``, falls back to the
    legacy direct-file read via ``_render_file_section``.
    """
    memory_dir = instance_root / "memory"
    wiki_dir = instance_root / "wiki"

    out: list[str] = []
    if (memory_dir / "INDEX.md").is_file():
        out.append(
            _render_file_section(memory_dir / "INDEX.md", label="Memory · INDEX.md")
        )

    pinned = _load_pinned_notes(memory_dir)
    if pinned:
        out.append("## Memory · Pinned atomic notes\n\n" + "\n\n---\n\n".join(pinned))

    # Wiki INDEX: route through CorpusBackend Protocol when available (PR 3
    # wiring). Both branches call _render_wiki_index_section with the same
    # logical path so output is byte-identical between corpus_backend=None
    # and corpus_backend=FilesystemCorpusBackend(...) (IRON RULE assertion 4).
    # Both branches apply .strip() to match _render_file_section's
    # _safe_read_text(...).strip() behavior. Skip the section when the
    # content is empty (no file or empty file), matching the existing
    # "skip empty wiki" behavior.
    wiki_label = "Wiki · INDEX.md"
    wiki_path = wiki_dir / "INDEX.md"
    if corpus_backend is not None:
        wiki_content = corpus_backend.render_index_summary(corpus="wiki").strip()
        if wiki_content:
            out.append(_render_wiki_index_section(wiki_label, wiki_path, wiki_content))
    else:
        if wiki_path.is_file():
            wiki_content = _safe_read_text(wiki_path).strip()
            if wiki_content:
                out.append(
                    _render_wiki_index_section(wiki_label, wiki_path, wiki_content)
                )

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
        p
        for p in memory_dir.glob("*.md")
        if p.name not in {"INDEX.md", "bundle.md"} and p.name not in pinned_names
    ]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    recent = candidates[:RECENT_NOTES_DEFAULT]

    if not recent:
        return []

    rendered = [f"# {p.name}\n\n{_safe_read_text(p)}" for p in recent]
    return [
        "# === BREAKPOINT 3: Session (recent atomic notes) ===",
        "## Recent atomic notes\n\n" + "\n\n---\n\n".join(rendered),
    ]


def _render_journal_breakpoint(
    instance_root: Path,
    journal_backend: "JournalBackend | None" = None,
) -> list[str]:
    """Render the recent journal entries (BP4), newest first.

    ADOPT-NOW (#427 PR1 — spec/43): routes through journal_backend.
    backend returns raw JournalEntry; formatting stays at this call site.
    bundle renders: '# Journal — {stem}\\n`{path}`\\n\\n{text}' (WITH path line).
    agent renders:  '# Journal — {stem}\\n\\n{text}' (NO path line).
    DO NOT unify — the divergence is LOAD-BEARING (byte-identity golden tests).
    """
    from .journal import get_default_journal_backend  # noqa: PLC0415

    _jbe = journal_backend or get_default_journal_backend(instance_root)
    journal_entries = _jbe.list_entries(limit=RECENT_JOURNAL_DEFAULT, newest_first=True)
    if not journal_entries:
        return []
    rendered = [
        f"# Journal — {entry.path.stem}\n`{entry.path}`\n\n{entry.text}"
        for entry in journal_entries
    ]
    return [
        "# === BREAKPOINT 4: Daily (recent journal) ===",
        "## Recent journal\n\n" + "\n\n---\n\n".join(rendered),
    ]


# ──────────────────────────────────────────────────────────────────
# Memory helpers


def _iter_pinned(memory_dir: Path):
    """Yield Paths to memory notes whose frontmatter contains ``pinned: true``.

    Cheap lexical check (no YAML parser) — matches the agent's own approach
    when scanning memory directories for the pinned filter. Accepts ``true``,
    ``yes``, or ``1`` as truthy. Strips inline ``# comment`` suffixes so
    ``pinned: true  # set by /pin-this`` still pins.
    """
    if not memory_dir.is_dir():
        return
    for path in sorted(memory_dir.glob("*.md")):
        if path.name == "INDEX.md":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
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
                value = stripped.split(":", 1)[1].strip()
                # Strip trailing inline comment so `pinned: true # set by op`
                # still parses as truthy. YAML treats `#` as a comment unless
                # quoted; we follow that convention with a cheap lexical strip.
                if "#" in value:
                    value = value.split("#", 1)[0].strip()
                if value.lower() in ("true", "yes", "1"):
                    yield path
                break


def _load_pinned_notes(memory_dir: Path, max_pinned: int = PINNED_MAX) -> list[str]:
    """Return rendered pinned atomic notes (full body, including frontmatter)."""
    notes: list[str] = []
    for path in _iter_pinned(memory_dir):
        notes.append(f"# {path.name}\n\n{_safe_read_text(path)}")
        if len(notes) >= max_pinned:
            break
    return notes


# NOTE: the former _load_recent_journal helper was removed in #427 PR1 — its
# storage logic now lives in FilesystemJournalBackend.list_entries() and its
# render logic in _render_journal_breakpoint (which routes through the backend).


# ──────────────────────────────────────────────────────────────────
# Rendering helpers


def _safe_read_text(path: Path) -> str:
    """Read a file as UTF-8; on UnicodeDecodeError or OSError, degrade gracefully.

    Bundle rendering should never crash because one source file has a stray
    non-UTF-8 byte, a permission error, or an OS-level read failure — operators
    want the rest of their bundle even when a single file is malformed.

    - On success: returns the file body verbatim.
    - On UnicodeDecodeError: re-reads with ``errors="replace"`` and prepends a
      warning comment so the operator can find the bad file.
    - On OSError (PermissionError, IsADirectoryError, etc.): returns just the
      warning comment naming the failure mode. No body content — there is none
      readable.
    """
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # Guard the re-read against a TOCTOU OSError (file deleted / perms
        # changed between reads) so the slot degrades-but-keeps. Kept
        # byte-identical to FilesystemJournalBackend._safe_read_entry (#464).
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"<!-- WARNING: {path.name} unreadable ({type(exc).__name__}). -->\n"
        return (
            f"<!-- WARNING: {path.name} contained non-UTF-8 bytes; replaced. -->\n"
            f"{body}"
        )
    except OSError as exc:
        # Name the failure mode (type only); never embed str(exc) — it carries
        # the absolute path, which would reach the LLM context via the journal.
        return f"<!-- WARNING: {path.name} unreadable ({type(exc).__name__}). -->\n"


def _render_file_section(path: Path, *, label: str) -> str:
    """Render one file as ``## {label}\\n`{path}`\\n\\n{body}``."""
    return f"## {label}\n`{path}`\n\n{_safe_read_text(path).strip()}"


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

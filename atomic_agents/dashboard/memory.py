"""Memory Snapshot tab — aggregation + render.

Answers "what does my fleet know?" — monthly review.

Reads memory/*.md frontmatter, INDEX.md, .versions/, and dreams manifests.
Pure Python, no LLM calls.
"""

from __future__ import annotations
import html
import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .costs import discover_agents
from ._shared import page_shell, truncate
from .._io import atomic_write
from ..memory.filesystem import FilesystemBackend

# Try to import python-frontmatter; if not available, use a simple fallback.
try:
    import frontmatter as _frontmatter
    _HAS_FRONTMATTER = True
except ImportError:
    _HAS_FRONTMATTER = False


# Known memory note types per spec/01
_KNOWN_TYPES = ("user", "feedback", "project", "decision", "reference")


# ──────────────────────────────────────────────────────────────────
# Data structures

@dataclass
class NoteTypeCount:
    agent: str
    user: int = 0
    feedback: int = 0
    project: int = 0
    decision: int = 0
    reference: int = 0
    other: int = 0

    @property
    def total(self) -> int:
        return self.user + self.feedback + self.project + self.decision + self.reference + self.other


@dataclass
class StalenessCandidate:
    agent: str
    note: str
    last_seen: str | None   # ISO date from frontmatter
    days_since: int | None
    pinned: bool


@dataclass
class OrphanNote:
    agent: str
    note: str   # filename in memory/ but absent from INDEX.md


@dataclass
class VersionChurnEntry:
    agent: str
    note: str           # note filename (stem.md)
    snapshot_count: int
    last_mutated: str   # ISO ts (derived from newest snapshot filename)


@dataclass
class DreamHistoryEntry:
    ts: str
    agent: str
    dream_id: str
    status: str
    consolidations: int
    promotions: int
    marked_stale: int
    applied: bool


@dataclass
class AgentMemorySize:
    agent: str
    live_bytes: int
    versions_bytes: int
    ratio: float   # versions / live (>1 means more in history than live)


@dataclass
class MemoryData:
    generated_at: datetime
    note_counts: list[NoteTypeCount]
    staleness_candidates: list[StalenessCandidate]
    orphan_notes: list[OrphanNote]
    version_churn: list[VersionChurnEntry]
    dream_history: list[DreamHistoryEntry]
    memory_sizes: list[AgentMemorySize]
    staleness_threshold_days: int


# ──────────────────────────────────────────────────────────────────
# Aggregation

def aggregate_memory(
    agents_root: Path,
    today: date | None = None,
    now: datetime | None = None,
    staleness_threshold_days: int = 90,
    version_churn_limit: int = 20,
) -> MemoryData:
    """Build MemoryData for the Memory Snapshot tab."""
    today = today or date.today()
    now = now or datetime.now(tz=timezone.utc)

    agent_names = discover_agents(agents_root)

    note_counts: list[NoteTypeCount] = []
    all_stale: list[StalenessCandidate] = []
    all_orphans: list[OrphanNote] = []
    all_churn: list[VersionChurnEntry] = []
    dream_history: list[DreamHistoryEntry] = []
    memory_sizes: list[AgentMemorySize] = []

    for agent in agent_names:
        agent_root = agents_root / agent
        memory_dir = agent_root / "memory"

        if not memory_dir.exists():
            continue

        # Use FilesystemBackend for all memory reads (protocol-compliant)
        backend = FilesystemBackend(agent_root, "memory")
        stats = backend.stats()

        # Note counts by type
        counts = NoteTypeCount(agent=agent)
        counts.user = stats.by_type.get("user", 0)
        counts.feedback = stats.by_type.get("feedback", 0)
        counts.project = stats.by_type.get("project", 0)
        counts.decision = stats.by_type.get("decision", 0)
        counts.reference = stats.by_type.get("reference", 0)
        # Any type not in the known set goes to "other"
        counts.other = sum(
            v for k, v in stats.by_type.items()
            if k not in _KNOWN_TYPES
        )

        if counts.total > 0:
            note_counts.append(counts)

        # Staleness via backend.list_stale()
        stale_refs = backend.list_stale(staleness_threshold_days, exclude_pinned=True)
        stale_notes: list[StalenessCandidate] = []
        for ref in stale_refs:
            days_since: int | None = None
            if ref.last_seen is not None:
                days_since = (today - ref.last_seen).days
            stale_notes.append(StalenessCandidate(
                agent=agent,
                note=ref.name,
                last_seen=ref.last_seen.isoformat() if ref.last_seen else None,
                days_since=days_since,
                pinned=ref.pinned,
            ))
        all_stale.extend(sorted(stale_notes, key=lambda x: x.days_since or 0, reverse=True))

        # Orphans via backend.list_orphans()
        orphan_refs = backend.list_orphans()
        for ref in orphan_refs:
            all_orphans.append(OrphanNote(agent=agent, note=ref.name))

        # Version churn via backend.stats().most_churned
        for note_name, snapshot_count in stats.most_churned:
            # Derive last_mutated from newest snapshot filename
            stem = note_name.replace(".md", "")
            versions_dir_path = memory_dir / ".versions" / stem
            last_mutated = ""
            if versions_dir_path.exists():
                snaps = sorted(versions_dir_path.glob("*.md"))
                if snaps:
                    last_snap = snaps[-1].name
                    last_mutated = last_snap[:15] if len(last_snap) >= 15 else last_snap
            all_churn.append(VersionChurnEntry(
                agent=agent,
                note=note_name,
                snapshot_count=snapshot_count,
                last_mutated=last_mutated,
            ))

        # Memory size via backend.stats()
        live_bytes = stats.live_bytes
        versions_bytes = stats.version_history_bytes
        ratio = versions_bytes / live_bytes if live_bytes > 0 else 0.0
        memory_sizes.append(AgentMemorySize(
            agent=agent,
            live_bytes=live_bytes,
            versions_bytes=versions_bytes,
            ratio=round(ratio, 2),
        ))

        # Dream history (no backend method — direct manifest scan)
        dreams_dir = agent_root / "dreams"
        if dreams_dir.exists():
            for dream_dir in sorted(dreams_dir.iterdir()):
                if not dream_dir.is_dir():
                    continue
                manifest_path = dream_dir / "manifest.json"
                if not manifest_path.exists():
                    continue
                try:
                    data = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                dream_history.append(DreamHistoryEntry(
                    ts=data.get("started_at", ""),
                    agent=agent,
                    dream_id=data.get("dream_id", dream_dir.name),
                    status=data.get("status", "unknown"),
                    consolidations=len(data.get("consolidated", [])),
                    promotions=len(data.get("promoted", [])),
                    marked_stale=len(data.get("marked_stale", [])),
                    applied=bool(data.get("applied_at")),
                ))

    # Sort version churn by snapshot count desc, take top N
    all_churn.sort(key=lambda x: x.snapshot_count, reverse=True)
    top_churn = all_churn[:version_churn_limit]

    # Sort dream history newest first
    dream_history.sort(key=lambda x: x.ts, reverse=True)

    # Sort stale notes by days_since desc (most stale first); sample up to 5 per agent
    # Group for display
    stale_sample: list[StalenessCandidate] = []
    agents_stale: dict[str, list[StalenessCandidate]] = {}
    for s in all_stale:
        agents_stale.setdefault(s.agent, []).append(s)
    for agent_stale_list in agents_stale.values():
        stale_sample.extend(agent_stale_list[:5])

    return MemoryData(
        generated_at=now,
        note_counts=note_counts,
        staleness_candidates=stale_sample,
        orphan_notes=all_orphans,
        version_churn=top_churn,
        dream_history=dream_history,
        memory_sizes=memory_sizes,
        staleness_threshold_days=staleness_threshold_days,
    )


def _read_frontmatter(path: Path) -> dict:
    """Parse YAML frontmatter from a .md file. Returns empty dict on failure."""
    if not _HAS_FRONTMATTER:
        return _simple_frontmatter_parse(path)
    try:
        parsed = _frontmatter.load(path)
        return dict(parsed.metadata)
    except Exception:
        return {}


def _simple_frontmatter_parse(path: Path) -> dict:
    """Minimal YAML frontmatter parser — only handles simple key: value lines."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    in_fm = False
    result: dict = {}
    for i, line in enumerate(lines):
        if i == 0 and line.strip() == "---":
            in_fm = True
            continue
        if in_fm and line.strip() == "---":
            break
        if in_fm and ":" in line:
            key, _, val = line.partition(":")
            result[key.strip()] = val.strip()
    return result


def _dir_size(path: Path, exclude_subdirs: bool = False) -> int:
    """Compute total bytes in a directory (optionally excluding subdirectory contents)."""
    if not path.exists():
        return 0
    total = 0
    try:
        for entry in os.scandir(path):
            if entry.is_file(follow_symlinks=False):
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
            elif entry.is_dir(follow_symlinks=False) and not exclude_subdirs:
                total += _dir_size(Path(entry.path))
    except OSError:
        pass
    return total


# ──────────────────────────────────────────────────────────────────
# Rendering

def render_memory(agents_root: Path, data: MemoryData) -> Path:
    """Write _dashboard/memory.html and return the path."""
    out_dir = agents_root / "_dashboard"
    out_dir.mkdir(parents=True, exist_ok=True)

    has_goals = any(
        (agents_root / agent / "goal.md").exists()
        for agent in discover_agents(agents_root)
    )
    html_content = _render_memory_template(data, has_goals=has_goals)
    out_path = out_dir / "memory.html"
    atomic_write(out_path, html_content)
    return out_path


def _fmt_bytes(n: int) -> str:
    """Human-readable byte count."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def _render_memory_template(data: MemoryData, has_goals: bool = True) -> str:
    # ── Note counts per agent per type
    if data.note_counts:
        rows = []
        for c in sorted(data.note_counts, key=lambda x: x.total, reverse=True):
            rows.append(
                f'<tr>'
                f'<td>{html.escape(c.agent)}</td>'
                f'<td class="right num">{c.user}</td>'
                f'<td class="right num">{c.feedback}</td>'
                f'<td class="right num">{c.project}</td>'
                f'<td class="right num">{c.decision}</td>'
                f'<td class="right num">{c.reference}</td>'
                f'<td class="right num">{c.other}</td>'
                f'<td class="right num"><strong>{c.total}</strong></td>'
                f'</tr>'
            )
        counts_table = (
            '<table>'
            '<thead><tr><th>Agent</th>'
            '<th class="right">User</th><th class="right">Feedback</th>'
            '<th class="right">Project</th><th class="right">Decision</th>'
            '<th class="right">Reference</th><th class="right">Other</th>'
            '<th class="right">Total</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody>'
            '</table>'
        )
    else:
        counts_table = '<p class="empty-note">No memory notes found across agents.</p>'

    # ── Staleness candidates
    thresh = data.staleness_threshold_days
    if data.staleness_candidates:
        rows = []
        for s in sorted(data.staleness_candidates, key=lambda x: x.days_since or 0, reverse=True):
            days_str = f"{s.days_since}d" if s.days_since is not None else "—"
            color = "var(--error)" if (s.days_since or 0) > 180 else "var(--warn)"
            rows.append(
                f'<tr>'
                f'<td>{html.escape(s.agent)}</td>'
                f'<td>{html.escape(s.note)}</td>'
                f'<td class="num">{html.escape(s.last_seen or "—")}</td>'
                f'<td class="right num" style="color: {color}">{days_str}</td>'
                f'</tr>'
            )
        stale_table = (
            '<table>'
            '<thead><tr><th>Agent</th><th>Note</th>'
            '<th>Last seen</th><th class="right">Age</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody>'
            '</table>'
        )
        stale_count = len(data.staleness_candidates)
        stale_intro = f'<p class="muted" style="margin-bottom: 12px">Notes last seen &gt; {thresh} days ago, not pinned. Showing up to 5 per agent ({stale_count} total).</p>'
    else:
        stale_table = f'<p class="empty-note">No staleness candidates (threshold: {thresh} days).</p>'
        stale_intro = ""

    # ── Orphan notes
    if data.orphan_notes:
        rows = []
        for o in data.orphan_notes:
            rows.append(
                f'<tr class="row-warn">'
                f'<td>{html.escape(o.agent)}</td>'
                f'<td>{html.escape(o.note)}</td>'
                f'</tr>'
            )
        orphan_table = (
            '<p class="muted" style="margin-bottom: 12px">These notes exist in memory/ but are not referenced in INDEX.md.</p>'
            '<table>'
            '<thead><tr><th>Agent</th><th>Note filename</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody>'
            '</table>'
        )
    else:
        orphan_table = '<p class="empty-note">No orphan notes detected. INDEX.md is in sync.</p>'

    # ── Version churn leaders
    if data.version_churn:
        rows = []
        for vc in data.version_churn:
            rows.append(
                f'<tr>'
                f'<td>{html.escape(vc.agent)}</td>'
                f'<td>{html.escape(vc.note)}</td>'
                f'<td class="right num"><strong>{vc.snapshot_count}</strong></td>'
                f'<td class="num">{html.escape(vc.last_mutated)}</td>'
                f'</tr>'
            )
        churn_table = (
            '<table>'
            '<thead><tr><th>Agent</th><th>Note</th>'
            '<th class="right">Snapshots</th><th>Last mutated</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody>'
            '</table>'
        )
    else:
        churn_table = '<p class="empty-note">No version history found.</p>'

    # ── Dream history
    if data.dream_history:
        rows = []
        for d in data.dream_history:
            applied_badge = (
                '<span class="pill ok">applied</span>'
                if d.applied
                else '<span class="pill neutral">pending</span>'
            )
            rows.append(
                f'<tr>'
                f'<td class="num">{html.escape(d.ts[:16] if d.ts else "—")}</td>'
                f'<td>{html.escape(d.agent)}</td>'
                f'<td class="muted">{html.escape(d.dream_id[:20])}</td>'
                f'<td><span class="pill neutral">{html.escape(d.status)}</span></td>'
                f'<td class="right num">{d.consolidations}</td>'
                f'<td class="right num">{d.promotions}</td>'
                f'<td class="right num">{d.marked_stale}</td>'
                f'<td>{applied_badge}</td>'
                f'</tr>'
            )
        dreams_table = (
            '<table>'
            '<thead><tr><th>Started</th><th>Agent</th><th>Dream ID</th><th>Status</th>'
            '<th class="right">Consol.</th><th class="right">Promoted</th>'
            '<th class="right">Staled</th><th>Applied</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody>'
            '</table>'
        )
    else:
        dreams_table = '<p class="empty-note">No dream history found.</p>'

    # ── Memory size + growth
    if data.memory_sizes:
        rows = []
        for ms in sorted(data.memory_sizes, key=lambda x: x.live_bytes, reverse=True):
            ratio_color = (
                "var(--error)" if ms.ratio > 5
                else "var(--warn)" if ms.ratio > 2
                else "var(--good)"
            )
            rows.append(
                f'<tr>'
                f'<td>{html.escape(ms.agent)}</td>'
                f'<td class="right num">{_fmt_bytes(ms.live_bytes)}</td>'
                f'<td class="right num">{_fmt_bytes(ms.versions_bytes)}</td>'
                f'<td class="right num" style="color: {ratio_color}">{ms.ratio:.1f}×</td>'
                f'</tr>'
            )
        size_table = (
            '<table>'
            '<thead><tr><th>Agent</th><th class="right">Live memory</th>'
            '<th class="right">Version history</th>'
            '<th class="right">History/Live ratio</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody>'
            '</table>'
        )
    else:
        size_table = '<p class="empty-note">No memory directories found.</p>'

    gen_ts = data.generated_at.strftime("%Y-%m-%d %H:%M UTC")
    body = f"""
<section class="panel">
  <h2>Note counts per agent per type</h2>
  {counts_table}
</section>

<section class="panel">
  <h2>Staleness candidates (&gt; {thresh} days, not pinned)</h2>
  {stale_intro}{stale_table}
</section>

<section class="panel">
  <h2>Orphan check — in memory/ but missing from INDEX.md</h2>
  {orphan_table}
</section>

<section class="panel">
  <h2>Version-churn leaders — top {len(data.version_churn)} most-versioned notes</h2>
  {churn_table}
</section>

<section class="panel">
  <h2>Dream history</h2>
  {dreams_table}
</section>

<section class="panel">
  <h2>Memory size + growth</h2>
  {size_table}
</section>
"""

    return page_shell(
        title="Memory Snapshot",
        body=body,
        current_tab="memory",
        has_goals=has_goals,
        subtitle=f"as of {gen_ts}",
    )

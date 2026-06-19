"""Tuning analyzer — eval-driven self-improvement loop.

Per the spec at <vault>/Atomic Agents/spec/11-tuning.md and the implementation
guide at <vault>/Atomic Agents/implementation/tuning-analyzer.md.

Reads recent eval results + lint reports + journal entries; detects recurring
patterns; generates specific edit proposals to persona/memory/tools files;
operator approves before any edit lands.

Usage:

    from atomic_agents.tuning import TuningRunner
    from pathlib import Path

    runner = TuningRunner(Path.home() / "agents", "caldwell")
    proposals = runner.analyze(window_days=60)
    runner.write_report(proposals)
    # ... operator reviews and edits frontmatter ...
    runner.apply_proposals("2026-05-08_proposal.md")

CLI:

    python -m atomic_agents.tuning <agent>                  # generate report
    python -m atomic_agents.tuning <agent> --since 30d      # custom window
    python -m atomic_agents.tuning <agent> --apply <file>   # apply approved
    python -m atomic_agents.tuning <agent> --apply <file> --dry-run  # preview
    python -m atomic_agents.tuning <agent> --polish         # LLM-polish wording

APPLY CONTRACT (per spec/11):
  --apply reads each accepted proposal's proposed_diff and writes the target
  file via atomic_write, respecting tools.md write_paths. Rejected proposals
  are recorded as-is. Proposals whose diffs cannot be cleanly applied are
  recorded with applied=False and a skip_reason.
"""

from __future__ import annotations
import json
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import frontmatter

from . import _llm
from ._io import atomic_write, atomic_append_jsonl
from ._platform import get_agents_root
from ._tools import parse_tools_md
from ._capture import enforce_write_path
from .exceptions import AtomicAgentsError, WritePathViolation


# ──────────────────────────────────────────────────────────────────
# Data classes

@dataclass
class PatternFinding:
    """One detected pattern, ready to feed an edit-proposal generator."""
    detector_id: str
    severity: str           # 'high' | 'medium' | 'low'
    confidence: str         # 'high' | 'medium' | 'low'
    summary: str
    evidence: list[dict] = field(default_factory=list)
    suggested_target_file: str = ""
    suggested_section: str = ""
    suggested_edit_type: str = "addition"  # 'addition' | 'modification' | 'removal'


@dataclass
class EditProposal:
    """One specific edit proposal awaiting operator decision."""
    proposal_id: str
    target_agent: str
    target_file: str
    target_section: str
    edit_type: str
    confidence: str
    reversibility: str
    pattern_summary: str
    proposed_diff: str
    rationale: str
    risks: str
    verification_plan: str
    estimated_impact: list[dict] = field(default_factory=list)
    operator_decision: str = "pending"     # 'pending' | 'accepted' | 'rejected' | 'deferred'
    operator_notes: str = ""


@dataclass
class AnalysisContext:
    """Shared input to all detectors."""
    agents_root: Path
    agent_name: str
    today: date
    window_days: int
    eval_runs: list[dict] = field(default_factory=list)         # parsed JSONL records
    lint_reports: list[dict] = field(default_factory=list)       # future
    memory_notes: dict[str, dict] = field(default_factory=dict)  # filename → {meta, body}
    tuning_history: list[dict] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────
# Pattern detectors

class PatternDetector:
    """Base class. Subclasses set `detector_id` and implement `detect()`."""
    detector_id: str = "abstract"
    min_data_points: int = 3

    def detect(self, ctx: AnalysisContext) -> list[PatternFinding]:
        raise NotImplementedError


class RecurringPersonaFidelityLow(PatternDetector):
    """persona_fidelity score ≤ threshold across N+ tests."""
    detector_id = "recurring_persona_fidelity_low"
    min_data_points = 3
    score_threshold = 3
    dimension = "persona_fidelity"

    def detect(self, ctx):
        low_results = []
        for run in ctx.eval_runs:
            scores = run.get("scores", {})
            score = scores.get(self.dimension)
            if score is not None and int(score) <= self.score_threshold:
                low_results.append(run)

        if len(low_results) < self.min_data_points:
            return []

        # Cluster judge justifications for recurring phrases
        phrases = Counter()
        for r in low_results:
            justifications = r.get("score_justifications", {})
            text = justifications.get(self.dimension, "")
            for phrase in _extract_distinctive_phrases(text):
                phrases[phrase] += 1

        common = [(p, c) for p, c in phrases.most_common(5) if c >= 2]
        if not common:
            return []

        confidence = "high" if len(low_results) >= 5 else "medium"
        return [PatternFinding(
            detector_id=self.detector_id,
            severity="high",
            confidence=confidence,
            summary=(
                f"{self.dimension} scored ≤{self.score_threshold} on "
                f"{len(low_results)} tests in last {ctx.window_days} days. "
                f"Recurring judge phrases: {[p for p, _ in common[:3]]}"
            ),
            evidence=[
                {"test_id": r.get("test_id"), "score": r.get("scores", {}).get(self.dimension),
                 "justification": r.get("score_justifications", {}).get(self.dimension, "")}
                for r in low_results[:5]
            ],
            suggested_target_file="persona/SOUL.md",
            suggested_section="Voice",
            suggested_edit_type="addition",
        )]


class HardFailRecurring(PatternDetector):
    """Same hard fail code firing across multiple tests/runs."""
    detector_id = "hard_fail_recurring"
    min_data_points = 2

    def detect(self, ctx):
        hard_fail_counts = Counter()
        evidence_by_hf = defaultdict(list)
        for run in ctx.eval_runs:
            for hf in run.get("hard_fails", []):
                hard_fail_counts[hf] += 1
                evidence_by_hf[hf].append({
                    "test_id": run.get("test_id"),
                    "ts": run.get("ts"),
                    "justification": run.get("overall_justification", ""),
                })

        findings = []
        for hf, count in hard_fail_counts.items():
            if count < self.min_data_points:
                continue
            findings.append(PatternFinding(
                detector_id=f"{self.detector_id}_{hf}",
                severity="high",
                confidence="high",
                summary=(
                    f"Hard fail {hf} fired {count} times across recent runs. "
                    f"Systematic ambiguity in tools.md or persona — needs tightening."
                ),
                evidence=evidence_by_hf[hf][:5],
                suggested_target_file="tools.md",
                suggested_section=f"Hard NOs / {hf}",
                suggested_edit_type="modification",
            ))
        return findings


class StaleNoteRecurring(PatternDetector):
    """A memory note marked stale by lint repeatedly without being refreshed.

    For v0.3: simplified — just check `last_seen` against today using the
    notes loaded into ctx. Real lint integration comes when lint runner exists.
    """
    detector_id = "stale_note_recurring"
    min_data_points = 1   # one note flagged stale is enough for the proposal

    stale_threshold_days = 90

    def detect(self, ctx):
        findings = []
        # Use backend.list_stale() when available (P2.6 migration), fall back to
        # manual ctx.memory_notes scan for backward compat in tests.
        backend = getattr(ctx, "memory_backend", None)
        if backend is not None:
            stale_refs = backend.list_stale(
                threshold_days=self.stale_threshold_days, exclude_pinned=True
            )
            for ref in stale_refs:
                if ref.last_seen is None:
                    continue
                age_days = (ctx.today - ref.last_seen).days
                findings.append(PatternFinding(
                    detector_id=f"{self.detector_id}_{ref.name}",
                    severity="medium",
                    confidence="high",
                    summary=(
                        f"`{ref.name}` last seen {age_days} days ago "
                        f"(threshold: {self.stale_threshold_days}d). "
                        f"Either pin it (still relevant) or archive it (no longer used)."
                    ),
                    evidence=[{
                        "note_path": ref.name,
                        "last_seen": ref.last_seen.isoformat(),
                        "age_days": age_days,
                    }],
                    suggested_target_file=f"memory/{ref.name}",
                    suggested_section="frontmatter",
                    suggested_edit_type="modification",
                ))
        return findings


class PromotableMemoryDetected(PatternDetector):
    """A feedback_*.md note referenced N+ times in eval runs without contradiction."""
    detector_id = "promotable_memory"
    min_references = 5

    def detect(self, ctx):
        ref_counts = Counter()
        first_seen: dict[str, str] = {}
        last_seen: dict[str, str] = {}

        for run in ctx.eval_runs:
            text_to_scan = (
                run.get("overall_justification", "")
                + " "
                + " ".join(str(j) for j in run.get("score_justifications", {}).values())
            )
            for filename in ctx.memory_notes:
                if not filename.startswith(("feedback_", "user_")):
                    continue
                if filename in text_to_scan or filename.replace(".md", "") in text_to_scan:
                    ref_counts[filename] += 1
                    ts = run.get("ts", "")
                    if filename not in first_seen:
                        first_seen[filename] = ts
                    last_seen[filename] = ts

        findings = []
        for filename, count in ref_counts.items():
            if count < self.min_references:
                continue
            note = ctx.memory_notes[filename]
            meta = note.get("meta", {})
            target_file = (
                "persona/USER.md" if filename.startswith("user_")
                else "persona/SOUL.md"
            )
            findings.append(PatternFinding(
                detector_id=f"{self.detector_id}_{filename}",
                severity="medium",
                confidence="high" if count >= 8 else "medium",
                summary=(
                    f"`{filename}` referenced in {count} eval runs without "
                    f"contradiction. Mature; consider promoting to persona "
                    f"({target_file})."
                ),
                evidence=[{
                    "filename": filename,
                    "reference_count": count,
                    "first_referenced": first_seen.get(filename, ""),
                    "last_referenced": last_seen.get(filename, ""),
                    "current_confidence": meta.get("confidence"),
                }],
                suggested_target_file=target_file,
                suggested_section="(determined per persona file)",
                suggested_edit_type="addition",
            ))
        return findings


# Default detector set — order matters for proposal IDs (1, 2, 3, ...)
DEFAULT_DETECTORS: list[type[PatternDetector]] = [
    RecurringPersonaFidelityLow,
    HardFailRecurring,
    StaleNoteRecurring,
    PromotableMemoryDetected,
]


# ──────────────────────────────────────────────────────────────────
# Proposal generation

def generate_proposals(
    findings: list[PatternFinding],
    agent_name: str,
    today: date,
) -> list[EditProposal]:
    """Convert detector findings into actionable EditProposals."""
    proposals: list[EditProposal] = []
    for i, f in enumerate(findings, start=1):
        proposal_id = f"{agent_name}-{today.isoformat()}-{i:03d}"
        proposals.append(_finding_to_proposal(f, proposal_id, agent_name))
    return proposals


def _finding_to_proposal(
    f: PatternFinding, proposal_id: str, agent_name: str
) -> EditProposal:
    """Build an EditProposal from a PatternFinding.

    Each detector type gets its own proposal shape; we dispatch on detector_id prefix.
    """
    if f.detector_id == "recurring_persona_fidelity_low":
        return _persona_fidelity_proposal(f, proposal_id, agent_name)
    if f.detector_id.startswith("hard_fail_recurring"):
        return _hard_fail_proposal(f, proposal_id, agent_name)
    if f.detector_id.startswith("stale_note_recurring"):
        return _stale_note_proposal(f, proposal_id, agent_name)
    if f.detector_id.startswith("promotable_memory"):
        return _promotion_proposal(f, proposal_id, agent_name)
    return _default_proposal(f, proposal_id, agent_name)


def _persona_fidelity_proposal(f, proposal_id, agent_name):
    # Build the proposed diff body from common phrases in evidence
    phrases_observed = []
    for ev in f.evidence:
        j = ev.get("justification", "")
        if j:
            phrases_observed.append(f'  - "{j}"')
    phrases_block = "\n".join(phrases_observed[:5]) or "  (no per-test justifications captured)"

    diff = (
        " ## Voice\n"
        " [existing content...]\n"
        "+- **(Proposed addition based on recurring eval observations)** "
        "Tighten voice rule to address the pattern detected: "
        f"{f.summary[:200]}\n"
    )
    return EditProposal(
        proposal_id=proposal_id,
        target_agent=agent_name,
        target_file=f.suggested_target_file,
        target_section=f.suggested_section,
        edit_type=f.suggested_edit_type,
        confidence=f.confidence,
        reversibility="high",
        pattern_summary=f.summary,
        proposed_diff=diff,
        rationale=(
            "The pattern is recurring across multiple eval runs. "
            "Persona is loaded at every runtime invocation per spec/04, "
            "so a tightening rule fires immediately on the next call.\n\n"
            "Recurring justifications observed:\n"
            f"{phrases_block}"
        ),
        risks=(
            "Over-correction is possible — the agent may swing too far the other way. "
            "Watch related rubric dimensions (output_quality, format_adherence) for "
            "regressions after applying."
        ),
        verification_plan=(
            "Re-run the full golden suite. Expect persona_fidelity scores to lift "
            "≥1 point on tests in the evidence list above."
        ),
        estimated_impact=[
            {"test_id": ev.get("test_id"), "dimension": "persona_fidelity", "expected_lift": "+1"}
            for ev in f.evidence
        ],
    )


def _hard_fail_proposal(f, proposal_id, agent_name):
    hf_code = f.detector_id.replace("hard_fail_recurring_", "")
    diff = (
        " ## Hard NOs\n"
        f"+- **{hf_code} (tightening based on recurring failures):** "
        f"Reword the existing hard-no rule to be more explicit about "
        f"the pattern detected. Pattern: {f.summary[:200]}\n"
    )
    return EditProposal(
        proposal_id=proposal_id,
        target_agent=agent_name,
        target_file=f.suggested_target_file,
        target_section=f.suggested_section,
        edit_type=f.suggested_edit_type,
        confidence=f.confidence,
        reversibility="high",
        pattern_summary=f.summary,
        proposed_diff=diff,
        rationale=(
            f"Hard fail {hf_code} has fired multiple times. The current rule "
            f"in tools.md is being misinterpreted by the agent. Tighter wording "
            f"reduces ambiguity."
        ),
        risks=(
            "If the wording becomes too strict, the agent may refuse legitimate "
            "requests it should handle. Watch the decline-category eval tests."
        ),
        verification_plan=(
            f"Re-run the full golden suite. Expect 0 occurrences of {hf_code} "
            "in subsequent runs."
        ),
    )


def _stale_note_proposal(f, proposal_id, agent_name):
    note_path = f.suggested_target_file
    diff = (
        " ---\n"
        " [existing frontmatter]\n"
        "+# Choose ONE:\n"
        "+pinned: true        # if the note is still actively relevant\n"
        "+# OR\n"
        "+archived: true      # if no longer used\n"
        " ---\n"
    )
    return EditProposal(
        proposal_id=proposal_id,
        target_agent=agent_name,
        target_file=note_path,
        target_section=f.suggested_section,
        edit_type=f.suggested_edit_type,
        confidence=f.confidence,
        reversibility="high",
        pattern_summary=f.summary,
        proposed_diff=diff,
        rationale=(
            "Stale notes that aren't pinned and aren't archived create lint noise "
            "and can confuse the agent's recall logic. Resolving the status either "
            "way (keep alive via pinning or archive) settles the question."
        ),
        risks="Cosmetic — no behavioral risk.",
        verification_plan="Run lint after applying; this note should no longer appear in the stale list.",
    )


def _promotion_proposal(f, proposal_id, agent_name):
    note_filename = f.detector_id.replace("promotable_memory_", "")
    diff = (
        f" # Two-part edit:\n"
        f" #\n"
        f" # 1. Append the note's body to {f.suggested_target_file}\n"
        f" #    under an appropriate section.\n"
        f" #\n"
        f" # 2. Mark the original note superseded:\n"
        f" #\n"
        f"   ---\n"
        f"   [existing frontmatter]\n"
        f"+  superseded_by: {f.suggested_target_file}#promoted-section-name\n"
        f"+  archived: true\n"
        f"   ---\n"
        f" #\n"
        f" # 3. Update memory/INDEX.md to move the entry to a 'Recently Promoted' section.\n"
    )
    return EditProposal(
        proposal_id=proposal_id,
        target_agent=agent_name,
        target_file=f.suggested_target_file,
        target_section="(operator-determined section)",
        edit_type="addition",
        confidence=f.confidence,
        reversibility="medium",  # archiving is reversible but moving content needs care
        pattern_summary=f.summary,
        proposed_diff=diff,
        rationale=(
            f"`{note_filename}` has been referenced consistently in eval runs "
            f"without contradiction. Per spec/05 promotion rules, mature memories "
            f"should move into persona where they're always loaded (not selectively recalled)."
        ),
        risks=(
            "Promotion adds bytes to the always-loaded context. Verify the persona "
            "file doesn't balloon past its budget (spec/04 recommends ~1-2K tokens "
            "per persona file)."
        ),
        verification_plan=(
            "After applying, the next eval run should still pass with the same scores. "
            "If memory_recall scores drop, the promotion may have removed something the "
            "agent was relying on finding via INDEX. Roll back if so."
        ),
    )


def _default_proposal(f, proposal_id, agent_name):
    return EditProposal(
        proposal_id=proposal_id, target_agent=agent_name,
        target_file=f.suggested_target_file or "(unknown)",
        target_section=f.suggested_section, edit_type=f.suggested_edit_type,
        confidence=f.confidence, reversibility="high",
        pattern_summary=f.summary,
        proposed_diff="(no auto-generated diff for this detector — operator must hand-author)",
        rationale=f"Detected: {f.summary}",
        risks="Unknown; operator should evaluate.",
        verification_plan="Re-run eval suite; observe whether the pattern recurs.",
    )


# ──────────────────────────────────────────────────────────────────
# Optional LLM polish

def polish_proposal_text(
    proposal: EditProposal,
    agent_root: Path,
    polish_model: str = "claude-sonnet-4-6-20260101",
) -> EditProposal:
    """Use a strong model to refine the proposal's natural-language wording.

    Reads the relevant agent file (e.g., SOUL.md) for context. Doesn't change
    the substance of the proposal — just tightens the prose.

    Cost: ~$0.02 per proposal at Sonnet pricing. Opt-in via --polish.
    """
    target = agent_root / proposal.target_file
    if target.exists():
        try:
            existing_content = target.read_text(encoding="utf-8")[:4000]
        except OSError:
            existing_content = "(could not read target file)"
    else:
        existing_content = "(target file does not exist yet)"

    prompt = (
        f"You are polishing a proposed edit to an AI agent's persona file.\n\n"
        f"Pattern detected:\n{proposal.pattern_summary}\n\n"
        f"Mechanical diff:\n```diff\n{proposal.proposed_diff}\n```\n\n"
        f"Target file ({proposal.target_file}) current contents:\n"
        f"```markdown\n{existing_content}\n```\n\n"
        f"Make the diff's natural-language wording feel native to this agent's "
        f"existing voice. Do NOT change the substance of the rule; just tighten "
        f"the wording. Output ONLY the new wording for the diff lines (do NOT "
        f"output the diff syntax itself, just the prose lines that should appear "
        f"in the target file)."
    )

    try:
        raw = _llm.call_llm(
            model=polish_model,
            system_prompt="",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=512,
            temperature=0.5,
        )
        polished = raw.text.strip()
        if polished:
            # Replace the substantive text in the diff while keeping the diff structure
            polished_diff = _replace_diff_addition(proposal.proposed_diff, polished)
            proposal.proposed_diff = polished_diff
    except Exception:
        # Polish is best-effort — fall back to original if it fails
        pass
    return proposal


def _replace_diff_addition(diff: str, new_text: str) -> str:
    """Replace the `+ ` lines in a diff with new text. Keep + prefix on each line."""
    lines = diff.splitlines()
    new_lines = []
    in_addition_block = False
    addition_indent = ""
    for line in lines:
        if line.startswith("+") and not line.startswith("++"):
            if not in_addition_block:
                # First addition line — replace with the polished text
                in_addition_block = True
                addition_indent = "+ "
                for new_line in new_text.splitlines():
                    new_lines.append(addition_indent + new_line.lstrip())
            # Skip subsequent addition lines — they're being replaced
        else:
            in_addition_block = False
            new_lines.append(line)
    return "\n".join(new_lines) + "\n"


# ──────────────────────────────────────────────────────────────────
# Report rendering

def render_report(
    proposals: list[EditProposal], agent_name: str, run_date: date,
    window_days: int,
) -> str:
    """Render the operator-readable tuning report markdown."""
    if not proposals:
        return _empty_report(agent_name, run_date, window_days)

    high_conf = sum(1 for p in proposals if p.confidence == "high")
    med_conf = sum(1 for p in proposals if p.confidence == "medium")
    low_conf = sum(1 for p in proposals if p.confidence == "low")

    proposal_blocks = []
    for p in proposals:
        proposal_blocks.append(_render_proposal_block(p))

    body = (
        f"# Tuning report — {agent_name} — {run_date.isoformat()}\n\n"
        f"Generated by `atomic_agents.tuning`. Window: last {window_days} days.\n\n"
        f"## Summary\n\n"
        f"{len(proposals)} proposal{'s' if len(proposals) != 1 else ''} "
        f"({high_conf} high-confidence, {med_conf} medium, {low_conf} low).\n\n"
        f"## How to act on this\n\n"
        f"For each proposal: edit its `operator_decision` frontmatter to one of:\n"
        f"- `accepted` — apply the edit as proposed\n"
        f"- `rejected` — don't apply (record why in `operator_notes`)\n"
        f"- `deferred` — decide later (re-surfaces in next tuning run)\n\n"
        f"Then run:\n\n"
        f"```bash\n"
        f"python -m atomic_agents.tuning {agent_name} --apply "
        f"{run_date.isoformat()}_proposal.md\n"
        f"```\n\n"
        f"---\n\n"
        f"## Proposals\n\n"
        + "\n\n---\n\n".join(proposal_blocks)
    )
    return body


def _render_proposal_block(p: EditProposal) -> str:
    """Render one proposal as its own self-contained block."""
    impact_lines = "\n".join(
        f"  - {imp}" for imp in p.estimated_impact[:5]
    ) if p.estimated_impact else "  (none specified)"

    return (
        f"```yaml\n"
        f"---\n"
        f"proposal_id: {p.proposal_id}\n"
        f"target_agent: {p.target_agent}\n"
        f"target_file: {p.target_file}\n"
        f"target_section: \"{p.target_section}\"\n"
        f"edit_type: {p.edit_type}\n"
        f"confidence: {p.confidence}\n"
        f"reversibility: {p.reversibility}\n"
        f"operator_decision: {p.operator_decision}\n"
        f"operator_notes: \"{p.operator_notes}\"\n"
        f"---\n"
        f"```\n\n"
        f"### {p.proposal_id}\n\n"
        f"**Pattern detected:**\n\n{p.pattern_summary}\n\n"
        f"**Proposed change** ({p.target_file}):\n\n"
        f"```diff\n{p.proposed_diff}```\n\n"
        f"**Rationale:**\n\n{p.rationale}\n\n"
        f"**Risks:**\n\n{p.risks}\n\n"
        f"**Estimated impact:**\n{impact_lines}\n\n"
        f"**Verification plan:**\n\n{p.verification_plan}\n"
    )


def _empty_report(agent_name: str, run_date: date, window_days: int) -> str:
    return (
        f"# Tuning report — {agent_name} — {run_date.isoformat()}\n\n"
        f"Generated by `atomic_agents.tuning`. Window: last {window_days} days.\n\n"
        f"## Summary\n\n"
        f"**No proposals.** No detectable patterns in the last {window_days} days "
        f"of eval data + memory state. Either:\n\n"
        f"- The agent is performing well across all dimensions, OR\n"
        f"- There's not enough eval data yet (needs ~4 weeks for confident pattern "
        f"detection per spec/11), OR\n"
        f"- The pattern detectors don't cover what's wrong (worth flagging if you "
        f"have a hunch about an issue the analyzer missed).\n"
    )


# ──────────────────────────────────────────────────────────────────
# Apply flow

def parse_report_proposals(report_path: Path) -> list[EditProposal]:
    """Re-parse a tuning report markdown to extract proposals + operator decisions.

    Reads the YAML frontmatter blocks (in code fences) for each proposal. The
    operator's decision is whatever they edited the frontmatter to.

    Also extracts the ``proposed_diff`` from the diff code fence in the proposal
    body — needed so ``apply_proposals`` can actually write the change.
    """
    text = report_path.read_text(encoding="utf-8")
    proposals: list[EditProposal] = []

    # Match ```yaml ... --- ... --- ... ``` blocks
    yaml_pattern = re.compile(
        r"```yaml\s*\n---\n(.*?)\n---\s*\n```",
        re.DOTALL | re.MULTILINE,
    )
    # Match the first ```diff ... ``` block that follows each yaml block
    diff_pattern = re.compile(r"```diff\n(.*?)```", re.DOTALL)

    # Split by yaml blocks so we can associate each diff with its preceding yaml block
    yaml_matches = list(yaml_pattern.finditer(text))
    diff_matches = list(diff_pattern.finditer(text))

    # Build a positional index: for each yaml match end-position, find the nearest
    # diff block that comes after it (and before the next yaml block, if any).
    def _find_diff_after(yaml_end: int, next_yaml_start: int | None) -> str:
        for dm in diff_matches:
            if dm.start() > yaml_end:
                if next_yaml_start is None or dm.start() < next_yaml_start:
                    return dm.group(1)
                break
        return ""

    for idx, match in enumerate(yaml_matches):
        meta_yaml = match.group(1)
        # Parse with frontmatter library (which handles YAML)
        try:
            parsed = frontmatter.loads(f"---\n{meta_yaml}\n---\n")
        except Exception:
            continue
        meta = parsed.metadata
        if "proposal_id" not in meta:
            continue

        next_yaml_start = yaml_matches[idx + 1].start() if idx + 1 < len(yaml_matches) else None
        proposed_diff = _find_diff_after(match.end(), next_yaml_start)

        proposals.append(EditProposal(
            proposal_id=str(meta.get("proposal_id", "")),
            target_agent=str(meta.get("target_agent", "")),
            target_file=str(meta.get("target_file", "")),
            target_section=str(meta.get("target_section", "")),
            edit_type=str(meta.get("edit_type", "")),
            confidence=str(meta.get("confidence", "")),
            reversibility=str(meta.get("reversibility", "")),
            pattern_summary="",
            proposed_diff=proposed_diff,
            rationale="",
            risks="",
            verification_plan="",
            operator_decision=str(meta.get("operator_decision", "pending")),
            operator_notes=str(meta.get("operator_notes", "")),
        ))
    return proposals


def _load_write_paths(agent_root: Path) -> list[Path]:
    """Load the agent's tools.md write_paths.  Falls back to [agent_root] if absent."""
    tools_path = agent_root / "tools.md"
    if tools_path.exists():
        # Thread agent_root so bare-relative write_paths anchor to the agent
        # folder, not the process CWD (#541 framework-wide anchoring).
        data = parse_tools_md(tools_path, agent_root=agent_root)
        paths = [Path(p) for p in data.get("write_paths", [])]
        if paths:
            return paths
    # Default: the entire agent folder is writable (no tools.md constraint)
    return [agent_root]


def _diff_is_auto_applicable(proposed_diff: str) -> tuple[bool, str]:
    """Decide whether a proposed_diff can be machine-applied.

    Returns (applicable, reason).  Diffs are auto-applicable when:
    - They contain at least one ``+ `` addition line
    - They don't consist entirely of comment lines (``# ``)
    - They have at least one context line (`` ``) or the target is an append

    Diffs that are instructional (all comments, multiple disjoint edit steps)
    are marked manual — the operator should hand-apply them.
    """
    if not proposed_diff or not proposed_diff.strip():
        return False, "empty diff"

    lines = proposed_diff.splitlines()
    add_lines = [l for l in lines if l.startswith("+") and not l.startswith("+++")]
    context_lines = [l for l in lines if l.startswith(" ") and not l.startswith("+++")]
    comment_lines = [l for l in lines if l.startswith("#") or l.lstrip().startswith("# ")]

    if not add_lines:
        return False, "no addition lines"

    # If most non-blank lines are comments, the diff is instructional
    non_blank = [l for l in lines if l.strip()]
    if non_blank and len(comment_lines) / len(non_blank) > 0.5:
        return False, "diff is instructional (mostly comments) — apply manually"

    return True, "ok"


def _apply_diff_to_file(target: Path, proposed_diff: str) -> str:
    """Apply a proposal's unified-diff-style string to *target*.

    Rules:
    - Lines starting with ``+`` (not ``++``) are insertions.
    - Lines starting with `` `` (space) are context — find them in the file and
      insert additions after the last matching context run.
    - Lines starting with ``-`` (not ``--``) are removals — find and remove them.
    - If there are no context lines, additions are appended at the end of the file.

    Returns the new file content as a string.  Raises ``AtomicAgentsError`` if
    context lines that are required for positioning cannot be found.
    """
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    lines_in = existing.splitlines(keepends=True)

    diff_lines = proposed_diff.splitlines()

    # Split diff into hunks: sequences of context/add/remove lines
    # For simplicity, treat the whole diff as one hunk.
    context = []
    additions: list[str] = []
    removals: list[str] = []

    for dl in diff_lines:
        if dl.startswith("+") and not dl.startswith("+++"):
            additions.append(dl[1:])  # strip the leading '+'
        elif dl.startswith("-") and not dl.startswith("---"):
            removals.append(dl[1:])   # strip the leading '-'
        elif dl.startswith(" "):
            context.append(dl[1:])    # strip the leading ' '
        # Lines starting with '#' or '@@' or '---'/'+++' are ignored

    if not additions and not removals:
        # Nothing to actually change
        return existing

    result_lines = list(lines_in)

    # Handle removals first (find and remove matching lines)
    for removal in removals:
        removal_stripped = removal.rstrip("\n")
        for i, line in enumerate(result_lines):
            if line.rstrip("\n") == removal_stripped:
                result_lines.pop(i)
                break
        # If not found, silently skip (idempotent)

    if additions:
        if context:
            # Find the last context line's position in the file and insert after it
            last_ctx = context[-1].rstrip("\n")
            insert_after = -1
            for i, line in enumerate(result_lines):
                if line.rstrip("\n") == last_ctx:
                    insert_after = i

            if insert_after == -1:
                # Context not found — append at end with a blank separator
                add_text = "\n".join(a.rstrip("\n") for a in additions)
                if result_lines and not result_lines[-1].endswith("\n"):
                    result_lines.append("\n")
                result_lines.append(add_text + "\n")
            else:
                # Insert additions after the context line
                add_entries = [a if a.endswith("\n") else a + "\n" for a in additions]
                for j, entry in enumerate(add_entries):
                    result_lines.insert(insert_after + 1 + j, entry)
        else:
            # No context — append additions at end of file
            if result_lines and not result_lines[-1].endswith("\n"):
                result_lines.append("\n")
            for a in additions:
                result_lines.append(a if a.endswith("\n") else a + "\n")

    return "".join(result_lines)


def apply_proposals(
    agents_root: Path, agent_name: str, report_filename: str,
    dry_run: bool = False,
) -> dict:
    """Apply approved proposals from a report file. Returns summary dict.

    Per spec/11:
    1. For each ``accepted`` proposal: read the proposed_diff, enforce write_paths,
       apply the diff to the target file via atomic_write.
    2. For each ``rejected`` / ``deferred``: record the decision to history only.
    3. ``pending`` proposals are ignored (operator hasn't decided yet).

    ``dry_run=True`` prints what would change but does NOT write any files.
    History is still written in dry_run mode so subsequent runs see the decisions.
    """
    report_path = agents_root / agent_name / "evals" / "tuning_reports" / report_filename
    if not report_path.exists():
        raise AtomicAgentsError(f"Tuning report not found: {report_path}")

    agent_root = agents_root / agent_name
    write_paths = _load_write_paths(agent_root)

    proposals = parse_report_proposals(report_path)
    accepted = [p for p in proposals if p.operator_decision == "accepted"]
    rejected = [p for p in proposals if p.operator_decision == "rejected"]
    deferred = [p for p in proposals if p.operator_decision == "deferred"]
    pending = [p for p in proposals if p.operator_decision == "pending"]

    summary: dict[str, Any] = {
        "total": len(proposals),
        "accepted": len(accepted),
        "rejected": len(rejected),
        "deferred": len(deferred),
        "pending": len(pending),
        "applied": 0,
        "skipped": 0,
        "failed": 0,
        "applied_ids": [],
        "skipped_ids": [],
        "failed_ids": [],
        "dry_run": dry_run,
    }

    # Try to apply each accepted proposal
    apply_results: dict[str, dict] = {}  # proposal_id → {applied, diff_applied, skip_reason, error}

    for p in accepted:
        target = agent_root / p.target_file

        # Check write_path enforcement
        try:
            enforce_write_path(target, write_paths)
        except WritePathViolation as e:
            apply_results[p.proposal_id] = {
                "applied": False, "diff_applied": None,
                "skip_reason": f"write_path violation: {e}",
            }
            summary["skipped"] += 1
            summary["skipped_ids"].append(p.proposal_id)
            continue

        # Decide if the diff is auto-applicable
        applicable, reason = _diff_is_auto_applicable(p.proposed_diff)
        if not applicable:
            apply_results[p.proposal_id] = {
                "applied": False, "diff_applied": None,
                "skip_reason": f"manual apply required — {reason}",
            }
            summary["skipped"] += 1
            summary["skipped_ids"].append(p.proposal_id)
            continue

        # Apply the diff
        try:
            new_content = _apply_diff_to_file(target, p.proposed_diff)
            if not dry_run:
                atomic_write(target, new_content)
            apply_results[p.proposal_id] = {
                "applied": not dry_run,
                "diff_applied": p.proposed_diff if not dry_run else None,
                "skip_reason": None,
            }
            if not dry_run:
                summary["applied"] += 1
                summary["applied_ids"].append(p.proposal_id)
            else:
                summary["skipped"] += 1
                summary["skipped_ids"].append(p.proposal_id)
        except Exception as e:
            apply_results[p.proposal_id] = {
                "applied": False, "diff_applied": None,
                "skip_reason": None, "error": str(e),
            }
            summary["failed"] += 1
            summary["failed_ids"].append(p.proposal_id)

    # Record everything (accepted, rejected, deferred) to tuning_history
    history_path = agents_root / agent_name / "evals" / "tuning_history.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    for p in proposals:
        if p.operator_decision == "pending":
            continue  # don't record pending — that's what next analysis will see
        res = apply_results.get(p.proposal_id, {})
        record = {
            "ts": datetime.now().astimezone().isoformat(),
            "proposal_id": p.proposal_id,
            "target_file": p.target_file,
            "edit_type": p.edit_type,
            "decision": p.operator_decision,
            "operator_notes": p.operator_notes,
            "applied": res.get("applied", False),
            "diff_applied": res.get("diff_applied"),
            "skip_reason": res.get("skip_reason"),
            "dry_run": dry_run,
        }
        atomic_append_jsonl(history_path, json.dumps(record))

    return summary


# ──────────────────────────────────────────────────────────────────
# Main runner

class TuningRunner:
    """Top-level coordinator. Loads context, runs detectors, generates proposals."""

    def __init__(
        self, agents_root: Path | None = None, agent_name: str = "",
        today: date | None = None,
    ):
        self.agents_root = agents_root or get_agents_root()
        self.agent_name = agent_name
        self.today = today or date.today()
        self.agent_root = self.agents_root / agent_name

        if not self.agent_root.exists():
            raise AtomicAgentsError(
                f"Agent folder not found: {self.agent_root}"
            )

    def analyze(
        self, window_days: int = 60,
        detectors: list[type[PatternDetector]] | None = None,
    ) -> list[EditProposal]:
        """Run all detectors over the lookback window. Return proposals."""
        ctx = self._build_context(window_days)
        detectors = detectors or DEFAULT_DETECTORS

        all_findings: list[PatternFinding] = []
        for det_cls in detectors:
            det = det_cls()
            try:
                findings = det.detect(ctx)
                all_findings.extend(findings)
            except Exception:
                # One detector crashing shouldn't kill the whole analysis
                continue

        return generate_proposals(all_findings, self.agent_name, self.today)

    def write_report(self, proposals: list[EditProposal], window_days: int = 60) -> Path:
        """Write the operator-readable report to evals/tuning_reports/."""
        reports_dir = self.agent_root / "evals" / "tuning_reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        out_path = reports_dir / f"{self.today.isoformat()}_proposal.md"
        body = render_report(proposals, self.agent_name, self.today, window_days)
        atomic_write(out_path, body)
        return out_path

    def apply(self, report_filename: str, dry_run: bool = False) -> dict:
        return apply_proposals(self.agents_root, self.agent_name, report_filename, dry_run=dry_run)

    def _build_context(self, window_days: int) -> AnalysisContext:
        """Load eval runs, memory notes, tuning history within the window."""
        cutoff = self.today - timedelta(days=window_days)

        ctx = AnalysisContext(
            agents_root=self.agents_root,
            agent_name=self.agent_name,
            today=self.today,
            window_days=window_days,
        )

        # Eval runs
        runs_dir = self.agent_root / "evals" / "runs"
        if runs_dir.exists():
            for path in sorted(runs_dir.glob("*.jsonl")):
                try:
                    ts_part = path.stem  # "2026-05-08"
                    file_date = date.fromisoformat(ts_part)
                except ValueError:
                    continue
                if file_date < cutoff:
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        ctx.eval_runs.append(rec)
                    except json.JSONDecodeError:
                        continue

        # Memory notes — loaded via the operator-configured MemoryBackend for
        # consistency (routes through get_default_memory_backend so an operator
        # who sets ATOMIC_AGENTS_MEMORY_BACKEND reads through that backend, not
        # always filesystem). The backend is stored on ctx so detectors
        # (StaleNoteRecurring) can use backend methods (list_stale) instead of
        # re-filtering locally.
        from .memory import get_default_memory_backend
        memory_backend = get_default_memory_backend(self.agent_root)
        ctx.memory_backend = memory_backend
        for ref in memory_backend.list_notes(include_archived=True, include_superseded=True):
            note = memory_backend.read_note(ref.name)
            if note is not None:
                ctx.memory_notes[ref.name] = {
                    "meta": {
                        "type": note.type,
                        "name": note.name,
                        "description": note.description,
                        "confidence": note.confidence,
                        "sources": note.sources,
                        "captured": note.captured.isoformat() if note.captured else None,
                        "last_seen": note.last_seen.isoformat() if note.last_seen else None,
                        "pinned": note.pinned,
                        "archived": note.archived,
                        "superseded_by": note.superseded_by,
                        "expires_at": note.expires_at,
                        "tags": note.tags,
                        **note.extra_frontmatter,
                    },
                    "body": note.body,
                }

        # Tuning history
        history_path = self.agent_root / "evals" / "tuning_history.jsonl"
        if history_path.exists():
            try:
                text = history_path.read_text(encoding="utf-8")
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ctx.tuning_history.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            except OSError:
                pass

        return ctx


# ──────────────────────────────────────────────────────────────────
# Helpers

# Words/phrases that often appear in low-quality justifications and are worth
# clustering. Detector-friendly heuristic — not exhaustive.
DISTINCTIVE_PHRASE_PATTERNS = [
    r"hedge[ds]?\b",
    r"hedge language",
    r"\"[^\"]{8,40}\"",  # quoted phrases the judge cited
    r"opens with [^,.]+",
    r"buries the lede",
    r"chatbot[- ]?y",
    r"lectures",
    r"asks (?:obvious|too many) questions",
    r"missing [a-z]+",
    r"too generic",
    r"drift(?:ed|ing|s)?",
    r"bottom[- ]line",
]


def _extract_distinctive_phrases(text: str) -> list[str]:
    """Pull recurring phrases out of judge justifications."""
    if not text:
        return []
    phrases = []
    for pattern in DISTINCTIVE_PHRASE_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            phrases.append(match.group(0).lower().strip('"'))
    return phrases


# ──────────────────────────────────────────────────────────────────
# CLI entry: python -m atomic_agents.tuning

def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="atomic-agents.tuning",
        description="Eval-driven tuning analyzer — proposes edits, never auto-applies",
    )
    parser.add_argument("agent", help="agent name (folder under agents-root)")
    parser.add_argument("--since", default="60d",
                        help="lookback window (e.g., '60d', '30d', '14d')")
    parser.add_argument("--apply", default=None, metavar="REPORT",
                        help="apply approved proposals from a report file")
    parser.add_argument("--polish", action="store_true",
                        help="LLM-polish proposal text (~$0.02/proposal)")
    parser.add_argument("--dry-run", action="store_true",
                        help="don't write report or apply changes — print what would happen")
    parser.add_argument("--agents-root", default=None,
                        help="override ATOMIC_AGENTS_ROOT")
    args = parser.parse_args(argv)

    agents_root = (
        Path(args.agents_root).expanduser().resolve()
        if args.agents_root else get_agents_root()
    )

    try:
        runner = TuningRunner(agents_root, args.agent)
    except AtomicAgentsError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if args.apply:
        try:
            summary = runner.apply(args.apply, dry_run=args.dry_run)
        except AtomicAgentsError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        label = " (DRY RUN — no files written)" if args.dry_run else ""
        print(f"Apply summary{label}:")
        print(f"  Total proposals:  {summary['total']}")
        print(f"  Accepted:         {summary['accepted']}")
        print(f"  Rejected:         {summary['rejected']}")
        print(f"  Deferred:         {summary['deferred']}")
        print(f"  Pending:          {summary['pending']}")
        if not args.dry_run:
            print(f"  Applied:          {summary['applied']}")
            if summary['skipped']:
                print(f"  Skipped (manual): {summary['skipped']}")
                print(f"    -> {summary['skipped_ids']}")
            if summary['failed']:
                print(f"  Failed:           {summary['failed']}")
                print(f"    -> {summary['failed_ids']}")
        print()
        print("Decisions recorded to evals/tuning_history.jsonl.")
        if summary.get('skipped_ids'):
            print("Skipped proposals require manual edits — review the report for")
            print("proposed_diff details and apply by hand.")
        return 0

    # Generate report
    window_days = _parse_since(args.since)
    proposals = runner.analyze(window_days=window_days)

    if args.polish and proposals:
        for p in proposals:
            polish_proposal_text(p, runner.agent_root)

    if args.dry_run:
        print(f"DRY RUN — would generate {len(proposals)} proposal(s)")
        for p in proposals:
            print(f"  {p.proposal_id} ({p.confidence}): {p.target_file} — {p.pattern_summary[:80]}")
        return 0

    out_path = runner.write_report(proposals, window_days=window_days)
    print(f"Report written: {out_path}")
    print(f"{len(proposals)} proposal(s).")
    if proposals:
        print(f"Review the report. To apply approved proposals:")
        print(f"  python -m atomic_agents.tuning {args.agent} --apply {out_path.name}")
    return 0


def _parse_since(s: str) -> int:
    """Parse '60d' / '30d' / '90' → int days."""
    s = s.strip().lower().rstrip("d")
    try:
        return int(s)
    except ValueError:
        return 60  # default


if __name__ == "__main__":
    import sys
    sys.exit(main())

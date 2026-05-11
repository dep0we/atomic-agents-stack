# Tuning Analyzer

How to build the Wave 6 tuning layer — `lib/atomic_agents/tuning.py` plus a `/tune` Claude Code skill wrapper. Spec is [../spec/11-tuning](../spec/11-tuning.md).

This is a **pattern-detection and edit-proposal generator.** Pure Python with one optional LLM call per proposal for natural-language drafting. No prompt-gradient optimization.

---

## Architecture

```
Eval JSONL files       Lint reports         Journal entries
       │                    │                      │
       └────────────────────┼──────────────────────┘
                            ▼
                    Pattern detectors
                            │
                            ▼
                    Edit proposal generators
                            │
                            ▼
                Optional: LLM polish on proposal text
                            │
                            ▼
              tuning_report.md (operator-readable)
                            │
                            ▼
             Operator marks accepted/rejected
                            │
                            ▼
                       Applier
                            │
                            ▼
                Edits land via shared helper
                (locked, validated, atomic)
```

---

## Pattern detectors

Each detector is a small Python class. New detectors get added as new patterns are discovered worth tracking.

```python
"""Pattern detectors for the tuning analyzer."""

from __future__ import annotations
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

@dataclass
class PatternFinding:
    """One detected pattern, ready to feed an edit-proposal generator."""
    detector_id: str
    severity: str                      # 'high' | 'medium' | 'low'
    confidence: str                    # 'high' | 'medium' | 'low'
    summary: str
    evidence: list[dict]               # raw data backing the finding
    suggested_target_file: str         # which agent file to edit
    suggested_section: str             # which section within that file
    suggested_edit_type: str           # 'addition' | 'modification' | 'removal'

class PatternDetector:
    """Base class for all detectors."""
    detector_id: str = "abstract"
    min_data_points: int = 3           # don't fire below this

    def detect(self, ctx: AnalysisContext) -> list[PatternFinding]:
        raise NotImplementedError


# ─── Persona / SOUL detectors ────────────────────────────────────

class RecurringPersonaFidelityLow(PatternDetector):
    """persona_fidelity scores ≤3 across N+ tests."""
    detector_id = "recurring_persona_fidelity_low"
    min_data_points = 3
    threshold_score = 3

    def detect(self, ctx):
        runs = ctx.eval_runs_recent(days=30)
        low_results = []
        for run in runs:
            for test in run.test_results:
                if test.scores.get("persona_fidelity", 5) <= self.threshold_score:
                    low_results.append(test)
        if len(low_results) < self.min_data_points:
            return []

        # Cluster judge justifications to find recurring phrases
        phrases = Counter()
        for r in low_results:
            for phrase in extract_distinctive_phrases(r.judge_justification):
                phrases[phrase] += 1

        common_phrases = [(p, c) for p, c in phrases.most_common() if c >= 2]
        if not common_phrases:
            return []

        return [PatternFinding(
            detector_id=self.detector_id,
            severity="high",
            confidence="high" if len(low_results) >= 5 else "medium",
            summary=f"persona_fidelity scored ≤{self.threshold_score} on "
                    f"{len(low_results)} tests in last 30 days. Recurring "
                    f"judge phrases: {[p for p, _ in common_phrases[:3]]}",
            evidence=[asdict(r) for r in low_results[:5]],
            suggested_target_file="persona/SOUL.md",
            suggested_section="Voice",
            suggested_edit_type="addition",
        )]


class HardFailRecurring(PatternDetector):
    """Same hard fail (HF1, HF5, etc.) firing across multiple tests/runs."""
    detector_id = "hard_fail_recurring"
    min_data_points = 2

    def detect(self, ctx):
        runs = ctx.eval_runs_recent(days=60)
        hard_fail_counts = Counter()
        evidence_by_hf = defaultdict(list)
        for run in runs:
            for test in run.test_results:
                for hf in test.hard_fails:
                    hard_fail_counts[hf] += 1
                    evidence_by_hf[hf].append({
                        "test_id": test.test_id,
                        "run_date": run.date,
                        "judge_justification": test.judge_justification,
                    })

        findings = []
        for hf, count in hard_fail_counts.items():
            if count >= self.min_data_points:
                findings.append(PatternFinding(
                    detector_id=f"{self.detector_id}_{hf}",
                    severity="high",
                    confidence="high",
                    summary=f"{hf} fired {count} times across recent runs — "
                            f"systematic ambiguity in tools.md or persona",
                    evidence=evidence_by_hf[hf][:5],
                    suggested_target_file="tools.md",
                    suggested_section=f"Hard NOs / {hf}",
                    suggested_edit_type="modification",
                ))
        return findings


# ─── Memory detectors ────────────────────────────────────────────

class StaleNoteRecurring(PatternDetector):
    """A note flagged stale by lint repeatedly without being refreshed."""
    detector_id = "stale_note_recurring"
    min_data_points = 3   # 3 lint reports flagging the same note

    def detect(self, ctx):
        lint_reports = ctx.lint_reports_recent(days=90)
        stale_counts = Counter()
        for report in lint_reports:
            for stale_note in report.stale:
                stale_counts[stale_note] += 1

        findings = []
        for note_path, count in stale_counts.items():
            if count >= self.min_data_points:
                findings.append(PatternFinding(
                    detector_id=f"{self.detector_id}_{note_path}",
                    severity="medium",
                    confidence="high",
                    summary=f"`{note_path}` has been marked stale {count} times "
                            f"in last 90 days without being refreshed. Either "
                            f"pin it (still relevant) or archive it (no longer used).",
                    evidence=[{"note_path": note_path, "stale_count": count}],
                    suggested_target_file=note_path,
                    suggested_section="frontmatter",
                    suggested_edit_type="modification",
                ))
        return findings


class PromotableMemoryDetected(PatternDetector):
    """A feedback_*.md note referenced N+ times without contradiction."""
    detector_id = "promotable_memory"
    min_data_points = 5

    def detect(self, ctx):
        # Cross-reference judge justifications (which often cite atomic notes)
        # against eval runs to find memories actively in use
        promotion_candidates = ctx.identify_promotion_candidates(threshold=self.min_data_points)

        findings = []
        for note in promotion_candidates:
            findings.append(PatternFinding(
                detector_id=f"{self.detector_id}_{note.filename}",
                severity="medium",
                confidence="high" if note.reference_count >= 8 else "medium",
                summary=f"`{note.filename}` referenced in {note.reference_count} runs "
                        f"over {note.span_days} days with no contradiction. Mature; "
                        f"consider promoting to persona.",
                evidence=[{"note": note.filename, "refs": note.reference_count}],
                suggested_target_file=f"persona/{note.suggested_persona_target}",
                suggested_section=note.suggested_persona_section,
                suggested_edit_type="addition",
            ))
        return findings


# ─── Format / scope detectors ────────────────────────────────────

class FormatLowOnSpecificInputShape(PatternDetector):
    """format_adherence drops on certain input patterns (long Qs, multi-part Qs)."""
    detector_id = "format_low_specific_shape"
    min_data_points = 3

    def detect(self, ctx):
        # Cluster failed-format tests by input shape (length, question count)
        # Look for "every long input fails format" patterns
        ...

class CostSpikeOnSpecificCategory(PatternDetector):
    """Helper opportunity — specific test category consistently expensive."""
    detector_id = "cost_spike_category"
    min_data_points = 4

    def detect(self, ctx):
        # Tests in category X consistently cost 3x average → consider helper
        ...
```

---

## Edit proposal generators

Each `PatternFinding` becomes one or more `EditProposal` objects. The generators turn detection into actionable diffs.

```python
@dataclass
class EditProposal:
    proposal_id: str
    target_agent: str
    target_file: str
    target_section: str
    edit_type: str
    confidence: str
    estimated_impact: list[dict]
    reversibility: str
    pattern_summary: str
    proposed_diff: str          # the actual edit to apply
    rationale: str               # human-readable why
    risks: str                   # what could go wrong
    verification_plan: str       # how to know it worked

class ProposalGenerator:
    """Base class for proposal generators."""
    handles_detector: str = "abstract"

    def generate(self, finding: PatternFinding,
                 agent_state: AgentState) -> list[EditProposal]:
        raise NotImplementedError

class HedgeLanguageProposalGenerator(ProposalGenerator):
    """For RecurringPersonaFidelityLow with 'hedge language' pattern."""
    handles_detector = "recurring_persona_fidelity_low"
    matches_phrases = ["hedge language", "hedges", "It really depends"]

    def generate(self, finding, agent_state):
        if not any(p in finding.summary for p in self.matches_phrases):
            return []

        diff = self._build_hedge_prohibition_diff(agent_state.soul_md)
        return [EditProposal(
            proposal_id=self._next_id(agent_state.name),
            target_agent=agent_state.name,
            target_file="persona/SOUL.md",
            target_section="Voice",
            edit_type="addition",
            confidence=finding.confidence,
            estimated_impact=self._estimate_impact(finding),
            reversibility="high",
            pattern_summary=finding.summary,
            proposed_diff=diff,
            rationale=("The hedge-opener pattern is mechanically detectable and the "
                       "fix is a single voice rule. Persona is loaded at every "
                       "runtime invocation per spec/04, so the new rule fires "
                       "immediately."),
            risks=("Caldwell may over-correct and respond with false certainty in "
                   "genuinely ambiguous cases. Watch output_quality on edge tests."),
            verification_plan="Re-run all golden tests; expect persona_fidelity ≥4.",
        )]

class PromotionProposalGenerator(ProposalGenerator):
    """For PromotableMemoryDetected — propose moving atomic note to persona."""
    handles_detector = "promotable_memory"

    def generate(self, finding, agent_state):
        # Read the candidate note; figure out where in persona it should land
        ...
```

---

## Optional LLM polish step

When a generator produces a `proposed_diff` that needs natural-language wording (e.g., the new SOUL.md bullet), it can call out to a strong model for polish:

```python
def polish_proposal_text(proposal: EditProposal, agent_state: AgentState) -> EditProposal:
    """Use Sonnet/GPT-5 to refine the proposal's natural-language sections."""
    prompt = f"""You are polishing a proposed edit to an AI agent's persona file.

The pattern detected:
{proposal.pattern_summary}

The mechanical diff:
{proposal.proposed_diff}

Make the diff's natural language feel native to this agent's existing voice
(see existing SOUL.md content below). Don't change the substance of the rule;
just tighten the wording.

Existing SOUL.md:
{agent_state.soul_md}

Output: just the new wording for the diff lines (do NOT output the diff syntax).
"""
    response = anthropic_client.messages.create(
        model="claude-sonnet-4-6-20260101",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    polished_text = response.content[0].text.strip()
    return _replace_diff_text(proposal, polished_text)
```

This is opt-in — the analyzer can produce machine-drafted proposals or polished ones. Polished proposals are nicer to read but cost ~$0.02 each.

---

## Report rendering

The analyzer writes a single markdown file per run:

```python
def render_report(proposals: list[EditProposal],
                  agent_name: str,
                  run_date: date) -> str:
    """Generate the operator-readable tuning_reports/YYYY-MM-DD_proposal.md content."""
    template = """# Tuning report — {agent_name} — {run_date}

Generated by atomic_agents.tuning.

## Summary

{summary_line}

## Proposals

{proposals_rendered}

## How to act on this

For each proposal: edit its frontmatter `operator_decision` to one of:
- `accepted` — apply the edit as proposed
- `rejected` — don't apply; record reason in `operator_notes`
- `deferred` — decide later (re-surfaces in next tuning run)

Then run:
    python -m atomic_agents.tuning {agent_name} --apply {filename}
"""
    proposals_rendered = "\n\n---\n\n".join(
        render_proposal(p) for p in proposals
    )
    summary = (f"{len(proposals)} proposal(s) — "
               f"{sum(1 for p in proposals if p.confidence == 'high')} high-confidence")
    return template.format(
        agent_name=agent_name,
        run_date=run_date,
        summary_line=summary,
        proposals_rendered=proposals_rendered,
    )

def render_proposal(p: EditProposal) -> str:
    """Render one proposal as a self-contained section."""
    return f"""---
proposal_id: {p.proposal_id}
target_agent: {p.target_agent}
target_file: {p.target_file}
target_section: {p.target_section}
edit_type: {p.edit_type}
confidence: {p.confidence}
reversibility: {p.reversibility}
operator_decision: pending
operator_notes: ""
---

# Proposal: {p.proposal_id}

## Pattern detected
{p.pattern_summary}

## Proposed change

```diff
{p.proposed_diff}
```

## Why this should work
{p.rationale}

## What could go wrong
{p.risks}

## Recommended verification
{p.verification_plan}
"""
```

---

## CLI entry point

```python
"""python -m atomic_agents.tuning <agent> [options]"""

import argparse
from pathlib import Path

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("agent")
    parser.add_argument("--since", default="60d", help="window for analysis (e.g., 60d)")
    parser.add_argument("--dry-run", action="store_true", help="don't write report file")
    parser.add_argument("--apply", help="apply approved proposals from a report file")
    parser.add_argument("--polish", action="store_true",
                        help="LLM-polish proposal text (~$0.02/proposal)")
    parser.add_argument("--agents-root", default=str(Path.home() / "docs" / "agents"))
    args = parser.parse_args()

    if args.apply:
        apply_proposals(args.agents_root, args.agent, args.apply)
        return

    runner = TuningRunner(Path(args.agents_root), args.agent)
    proposals = runner.analyze(since=args.since)
    if args.polish:
        proposals = [polish_proposal_text(p, runner.agent_state) for p in proposals]
    if args.dry_run:
        print(f"Would write {len(proposals)} proposals to "
              f"{runner.report_path_for_today}")
    else:
        runner.write_report(proposals)
        print(f"Report written to {runner.report_path_for_today}")
        print(f"{len(proposals)} proposal(s); review and run "
              f"`python -m atomic_agents.tuning {args.agent} --apply ...` to land changes")
```

Sample output:

```
Tuning analysis for caldwell — looking back 60d
═══════════════════════════════════════════════════════════════

Patterns detected: 3
  ▸ Recurring persona_fidelity drops (4 tests, 30d) — high confidence
  ▸ Stale note: feedback_communication_style.md flagged 4× (90d) — medium
  ▸ Promotable memory: user_money_stress.md referenced 7× (45d) — high

Proposals generated: 3 (2 high-confidence, 1 medium)

Report written to:
  ~/agents/caldwell/evals/tuning_reports/2026-05-08_proposal.md

Review the report. To apply approved proposals:
  python -m atomic_agents.tuning caldwell --apply 2026-05-08_proposal.md

Total analysis cost: $0.06 (3 LLM polish calls)
```

---

## The `/tune` Claude Code skill

Thin wrapper, same pattern as `/eval`:

```yaml
---
name: tune
description: Run tuning analysis on an Atomic Agent. Reads recent eval results,
  lint reports, and journal entries; detects recurring patterns; proposes specific
  edits to persona/memory/tools files. Operator approves before any edit lands.
  Use after evals show consistent issues, or weekly as part of agent maintenance.
---

# Atomic Agents — Tune skill

When invoked:
1. Ask which agent (or take as `/tune caldwell`)
2. Optionally ask for a window (default 60d)
3. Optionally ask whether to polish proposal text via LLM (~$0.02/proposal, default no)
4. Run via Bash:
    python -m atomic_agents.tuning $AGENT --since $WINDOW [--polish]
5. After completion, read the generated report file and surface:
    - Number of proposals
    - For each: target_file, edit_type, confidence, one-sentence summary
    - Rendered diff (the actual proposed change)
6. Ask the operator whether to mark proposals accepted/rejected interactively
7. If yes, edit the frontmatter on each proposal as decided
8. Run apply step:
    python -m atomic_agents.tuning $AGENT --apply $REPORT_FILE
9. Report what landed
```

---

## Application flow

```python
def apply_proposals(agents_root: Path, agent_name: str, report_filename: str):
    """Apply all `accepted` proposals from a report; record decisions."""
    report_path = agents_root / agent_name / "evals" / "tuning_reports" / report_filename
    proposals = parse_report(report_path)

    accepted = [p for p in proposals if p.operator_decision == "accepted"]
    rejected = [p for p in proposals if p.operator_decision == "rejected"]
    deferred = [p for p in proposals if p.operator_decision == "deferred"]

    helper = AtomicAgent(name=agent_name, vault_root=agents_root, trigger="tune")

    applied = []
    failed = []
    for p in accepted:
        try:
            target = agents_root / agent_name / p.target_file
            apply_diff(target, p.proposed_diff, helper)  # uses helper's lock + atomic write
            applied.append(p.proposal_id)
        except Exception as e:
            failed.append((p.proposal_id, str(e)))

    # Record everything to tuning_history
    history_path = agents_root / agent_name / "evals" / "tuning_history.jsonl"
    with history_path.open("a") as f:
        for p in proposals:
            line = {
                "ts": datetime.now().isoformat(),
                "proposal_id": p.proposal_id,
                "target_file": p.target_file,
                "edit_type": p.edit_type,
                "decision": p.operator_decision,
                "operator_notes": p.operator_notes,
                "applied": p.proposal_id in applied,
                "diff_applied": p.proposed_diff if p.proposal_id in applied else None,
            }
            f.write(json.dumps(line) + "\n")

    # Append a journal entry
    helper.append_journal({
        "what_happened": f"Tuning applied: {len(applied)} accepted, {len(rejected)} rejected, {len(deferred)} deferred",
        "applied_proposals": applied,
        "failed_proposals": failed,
    })

    print(f"Applied {len(applied)}/{len(accepted)} proposals.")
    if failed:
        print(f"⚠ {len(failed)} proposals failed:")
        for pid, err in failed:
            print(f"  {pid}: {err}")
```

The `apply_diff` function uses the standard helper's atomic-write protocol — temp file, fsync, rename, lock — same path as captures. No special-case write logic for tuning.

---

## When the analyzer is wrong

The analyzer will sometimes propose bad edits:

- **False pattern**: detector fires on noise. Fix: tune the detector's `min_data_points` higher, or add a confidence penalty.
- **Right pattern, wrong edit**: detector found something real, but the proposed change wouldn't actually fix it. Fix: improve the proposal generator, OR mark this case for manual handling in the rejection notes ("good catch but the right fix is X, not Y").
- **Conflicting proposals**: two proposals that contradict each other (one says add hedge prohibition, another says soften certainty). Fix: the analyzer should detect conflicts before rendering and surface them as a single "needs operator judgment" item.

Improving the analyzer is itself an iterative process. Track:
- Acceptance rate per detector (which detectors produce useful proposals?)
- Post-apply score lift (do accepted proposals actually move scores?)
- Re-rejection rate (are we proposing things that get rejected repeatedly?)

After a few months, prune detectors that have <30% acceptance rate.

---

## What's NOT in this implementation

- **Auto-apply mode** — every change is operator-approved. No `--auto-apply` flag exists.
- **Rubric tuning** — the rubric is what tuning targets; tuning doesn't tune itself.
- **Cross-agent learning** — tuning analyzes one agent at a time. A personal-scale setup is small enough that cross-agent pattern transfer (e.g., "Caldwell got better with X; try X on agent-a") would be over-engineering. v2 if needed.
- **Real-time tuning** — tuning runs after evals (post-hoc). It doesn't observe and tune mid-conversation.

---

*See [../spec/11-tuning](../spec/11-tuning.md) for the spec, [samples/caldwell/evals/tuning_reports/](../samples/caldwell/evals/tuning_reports/2026-05-08_proposal.md) for a worked example.*

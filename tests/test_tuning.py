"""Tests for atomic_agents.tuning."""

from __future__ import annotations
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import frontmatter
import pytest

from atomic_agents.tuning import (
    TuningRunner,
    AnalysisContext,
    EditProposal,
    PatternFinding,
    HardFailRecurring,
    PromotableMemoryDetected,
    RecurringPersonaFidelityLow,
    StaleNoteRecurring,
    apply_proposals,
    generate_proposals,
    parse_report_proposals,
    polish_proposal_text,
    render_report,
    _apply_diff_to_file,
    _diff_is_auto_applicable,
    _extract_distinctive_phrases,
    _parse_since,
)
from atomic_agents.exceptions import AtomicAgentsError, WritePathViolation


# ──────────────────────────────────────────────────────────────────
# Fixtures

@pytest.fixture
def agent_with_data(tmp_path):
    """An agent vault with eval runs and memory notes."""
    agents_root = tmp_path / "agents"
    agent_root = agents_root / "testagent"

    runs_dir = agent_root / "evals" / "runs"
    runs_dir.mkdir(parents=True)
    memory_dir = agent_root / "memory"
    memory_dir.mkdir()

    today = date(2026, 5, 8)

    # Write 5 eval runs over the past 30 days, all with persona_fidelity=3
    # and the same hedge-language judge phrase
    for i in range(5):
        run_date = today - timedelta(days=i * 5)
        line = json.dumps({
            "ts": datetime.combine(run_date, datetime.min.time()).isoformat(),
            "agent": "testagent",
            "test_id": f"test_{i:03d}",
            "category": "happy",
            "agent_model": "claude-sonnet-4-6-20260101",
            "judge_model": "gpt-5",
            "scores": {"persona_fidelity": 3, "output_quality": 4},
            "score_justifications": {
                "persona_fidelity": f"Opens with 'It really depends...' — hedge language",
                "output_quality": "Good reasoning",
            },
            "weighted_score": 3.5,
            "hard_fails": [],
            "verdict": "fail",
            "overall_justification": "Persona drift via hedge openers; references feedback_communication.md",
        })
        log_path = runs_dir / f"{run_date.isoformat()}.jsonl"
        log_path.write_text(line + "\n")

    # Memory notes: one stale, one promotable
    stale_note = memory_dir / "feedback_old_thing.md"
    stale_note.write_text(frontmatter.dumps(frontmatter.Post(
        "Old advice that's not relevant anymore.",
        schema_version=1,
        name="Old thing",
        description="Stale",
        type="feedback",
        captured="2025-01-01",
        last_seen="2025-09-01",  # 245 days before May 8, 2026
        sources=["conversation_2025-01-01"],
        confidence="medium",
    )) + "\n")

    promotable = memory_dir / "feedback_communication.md"
    promotable.write_text(frontmatter.dumps(frontmatter.Post(
        "Bottom-line first.",
        schema_version=1,
        name="Bottom line first",
        description="Always lead with the recommendation",
        type="feedback",
        captured="2026-04-01",
        last_seen="2026-05-08",
        sources=["conversation_2026-04-01"],
        confidence="high",
    )) + "\n")

    return agents_root, "testagent", today


@pytest.fixture
def agent_no_data(tmp_path):
    """Empty agent — just the folder, no eval runs."""
    agents_root = tmp_path / "agents"
    agent_root = agents_root / "emptyagent"
    agent_root.mkdir(parents=True)
    (agent_root / "memory").mkdir()
    (agent_root / "evals").mkdir()
    return agents_root, "emptyagent"


# ──────────────────────────────────────────────────────────────────
# Helpers

def test_extract_distinctive_phrases_finds_hedge():
    text = "Opens with 'It really depends...' — hedge language"
    phrases = _extract_distinctive_phrases(text)
    assert any("hedge" in p for p in phrases)


def test_extract_distinctive_phrases_empty():
    assert _extract_distinctive_phrases("") == []
    assert _extract_distinctive_phrases("nothing notable here") == []


def test_parse_since_basic():
    assert _parse_since("60d") == 60
    assert _parse_since("30") == 30
    assert _parse_since("90D") == 90


def test_parse_since_invalid_falls_back():
    assert _parse_since("garbage") == 60


# ──────────────────────────────────────────────────────────────────
# Pattern detector tests

def test_recurring_persona_fidelity_low_detects(agent_with_data):
    agents_root, agent_name, today = agent_with_data
    runner = TuningRunner(agents_root, agent_name, today=today)
    ctx = runner._build_context(window_days=30)

    detector = RecurringPersonaFidelityLow()
    findings = detector.detect(ctx)
    assert len(findings) == 1
    assert findings[0].confidence in ("high", "medium")
    assert "persona_fidelity" in findings[0].summary


def test_recurring_persona_fidelity_low_doesnt_fire_with_few_results(agent_no_data):
    agents_root, agent_name = agent_no_data
    runner = TuningRunner(agents_root, agent_name, today=date(2026, 5, 8))
    ctx = runner._build_context(window_days=30)
    detector = RecurringPersonaFidelityLow()
    assert detector.detect(ctx) == []


def test_hard_fail_recurring_detects(tmp_path):
    agents_root = tmp_path / "agents"
    agent_root = agents_root / "a"
    runs = agent_root / "evals" / "runs"
    runs.mkdir(parents=True)
    today = date(2026, 5, 8)
    for i in range(3):
        d = today - timedelta(days=i)
        runs.joinpath(f"{d.isoformat()}.jsonl").write_text(json.dumps({
            "ts": datetime.combine(d, datetime.min.time()).isoformat(),
            "test_id": f"t{i}",
            "hard_fails": ["HF1"],
            "overall_justification": "leaked secrets",
        }) + "\n")
    runner = TuningRunner(agents_root, "a", today=today)
    ctx = runner._build_context(window_days=30)
    findings = HardFailRecurring().detect(ctx)
    assert len(findings) == 1
    assert "HF1" in findings[0].detector_id
    assert findings[0].confidence == "high"


def test_hard_fail_recurring_doesnt_fire_with_one_occurrence(tmp_path):
    agents_root = tmp_path / "agents"
    agent_root = agents_root / "a"
    runs = agent_root / "evals" / "runs"
    runs.mkdir(parents=True)
    today = date(2026, 5, 8)
    runs.joinpath(f"{today.isoformat()}.jsonl").write_text(json.dumps({
        "ts": datetime.combine(today, datetime.min.time()).isoformat(),
        "hard_fails": ["HF1"],
    }) + "\n")
    runner = TuningRunner(agents_root, "a", today=today)
    ctx = runner._build_context(window_days=30)
    assert HardFailRecurring().detect(ctx) == []


def test_stale_note_detector(agent_with_data):
    agents_root, agent_name, today = agent_with_data
    runner = TuningRunner(agents_root, agent_name, today=today)
    ctx = runner._build_context(window_days=30)
    findings = StaleNoteRecurring().detect(ctx)
    assert len(findings) == 1  # only the stale one
    assert "feedback_old_thing.md" in findings[0].summary


def test_promotable_memory_detector(agent_with_data):
    agents_root, agent_name, today = agent_with_data
    runner = TuningRunner(agents_root, agent_name, today=today)
    ctx = runner._build_context(window_days=30)
    findings = PromotableMemoryDetected().detect(ctx)
    assert len(findings) == 1
    assert "feedback_communication.md" in findings[0].detector_id


def test_promotable_memory_doesnt_fire_below_threshold(tmp_path):
    """Need 5+ references for promotion."""
    agents_root = tmp_path / "agents"
    agent_root = agents_root / "a"
    (agent_root / "memory").mkdir(parents=True)
    runs = agent_root / "evals" / "runs"
    runs.mkdir(parents=True)
    today = date(2026, 5, 8)

    note = (agent_root / "memory" / "feedback_x.md")
    note.write_text(frontmatter.dumps(frontmatter.Post(
        "x", schema_version=1, name="x", description="x", type="feedback",
        captured="2026-04-01", last_seen="2026-05-08", sources=["s1"], confidence="high",
    )) + "\n")

    # Only 3 references — under the 5 threshold
    for i in range(3):
        d = today - timedelta(days=i)
        runs.joinpath(f"{d.isoformat()}.jsonl").write_text(json.dumps({
            "ts": datetime.combine(d, datetime.min.time()).isoformat(),
            "overall_justification": "uses feedback_x",
        }) + "\n")

    runner = TuningRunner(agents_root, "a", today=today)
    ctx = runner._build_context(window_days=30)
    assert PromotableMemoryDetected().detect(ctx) == []


# ──────────────────────────────────────────────────────────────────
# Proposal generation

def test_generate_proposals_assigns_unique_ids(agent_with_data):
    agents_root, agent_name, today = agent_with_data
    runner = TuningRunner(agents_root, agent_name, today=today)
    proposals = runner.analyze(window_days=30)
    ids = [p.proposal_id for p in proposals]
    assert len(ids) == len(set(ids))


def test_generate_proposals_have_required_fields(agent_with_data):
    agents_root, agent_name, today = agent_with_data
    runner = TuningRunner(agents_root, agent_name, today=today)
    proposals = runner.analyze(window_days=30)
    for p in proposals:
        assert p.proposal_id
        assert p.target_agent == agent_name
        assert p.target_file
        assert p.confidence in ("high", "medium", "low")
        assert p.proposed_diff
        assert p.rationale
        assert p.operator_decision == "pending"


def test_generate_proposals_empty_when_no_findings(agent_no_data):
    agents_root, agent_name = agent_no_data
    runner = TuningRunner(agents_root, agent_name, today=date(2026, 5, 8))
    proposals = runner.analyze()
    assert proposals == []


# ──────────────────────────────────────────────────────────────────
# Report rendering

def test_render_report_with_proposals():
    proposals = [EditProposal(
        proposal_id="agent-2026-05-08-001",
        target_agent="agent",
        target_file="persona/SOUL.md",
        target_section="Voice",
        edit_type="addition",
        confidence="high",
        reversibility="high",
        pattern_summary="Hedge language detected 4×",
        proposed_diff="+ no hedging\n",
        rationale="Recurring pattern.",
        risks="Over-correction.",
        verification_plan="Re-run suite.",
    )]
    report = render_report(proposals, "agent", date(2026, 5, 8), 60)
    assert "1 proposal" in report
    assert "agent-2026-05-08-001" in report
    assert "operator_decision: pending" in report
    assert "persona/SOUL.md" in report


def test_render_report_empty():
    report = render_report([], "agent", date(2026, 5, 8), 60)
    assert "No proposals" in report
    assert "No detectable patterns" in report


def test_write_report_creates_file(agent_with_data):
    agents_root, agent_name, today = agent_with_data
    runner = TuningRunner(agents_root, agent_name, today=today)
    proposals = runner.analyze(window_days=30)
    out_path = runner.write_report(proposals)
    assert out_path.exists()
    assert out_path.name == f"{today.isoformat()}_proposal.md"
    text = out_path.read_text()
    assert agent_name in text


# ──────────────────────────────────────────────────────────────────
# Apply flow

def test_parse_report_proposals_round_trip(tmp_path):
    """Write a report, read it back — proposal frontmatter survives."""
    proposals = [EditProposal(
        proposal_id="x-001", target_agent="x", target_file="a.md",
        target_section="b", edit_type="addition", confidence="high",
        reversibility="high", pattern_summary="p", proposed_diff="d",
        rationale="r", risks="r2", verification_plan="v",
        operator_decision="accepted",  # operator changed this
    )]
    body = render_report(proposals, "x", date(2026, 5, 8), 60)
    report_path = tmp_path / "test_report.md"
    report_path.write_text(body)

    parsed = parse_report_proposals(report_path)
    assert len(parsed) == 1
    assert parsed[0].proposal_id == "x-001"
    assert parsed[0].operator_decision == "accepted"


def test_apply_records_decisions_to_history(agent_with_data, tmp_path):
    agents_root, agent_name, today = agent_with_data

    # Write a report with mixed decisions
    proposals = [
        EditProposal(
            proposal_id=f"{agent_name}-{today.isoformat()}-001",
            target_agent=agent_name, target_file="persona/SOUL.md",
            target_section="Voice", edit_type="addition",
            confidence="high", reversibility="high",
            pattern_summary="x", proposed_diff="d", rationale="r",
            risks="r", verification_plan="v",
            operator_decision="accepted",
        ),
        EditProposal(
            proposal_id=f"{agent_name}-{today.isoformat()}-002",
            target_agent=agent_name, target_file="memory/x.md",
            target_section="frontmatter", edit_type="modification",
            confidence="medium", reversibility="high",
            pattern_summary="x", proposed_diff="d", rationale="r",
            risks="r", verification_plan="v",
            operator_decision="rejected", operator_notes="noisy",
        ),
        EditProposal(
            proposal_id=f"{agent_name}-{today.isoformat()}-003",
            target_agent=agent_name, target_file="x", target_section="x",
            edit_type="addition", confidence="low", reversibility="high",
            pattern_summary="x", proposed_diff="d", rationale="r",
            risks="r", verification_plan="v",
            operator_decision="pending",  # not yet decided
        ),
    ]
    body = render_report(proposals, agent_name, today, 60)
    reports_dir = agents_root / agent_name / "evals" / "tuning_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"{today.isoformat()}_proposal.md"
    report_path.write_text(body)

    summary = apply_proposals(agents_root, agent_name, report_path.name)
    assert summary["total"] == 3
    assert summary["accepted"] == 1
    assert summary["rejected"] == 1
    assert summary["pending"] == 1

    # tuning_history.jsonl should have 2 records (accepted + rejected, NOT pending)
    history_path = agents_root / agent_name / "evals" / "tuning_history.jsonl"
    assert history_path.exists()
    lines = history_path.read_text().strip().split("\n")
    assert len(lines) == 2
    decisions = sorted(json.loads(l)["decision"] for l in lines)
    assert decisions == ["accepted", "rejected"]


def test_apply_missing_report_raises(agent_with_data):
    agents_root, agent_name, today = agent_with_data
    with pytest.raises(AtomicAgentsError, match="not found"):
        apply_proposals(agents_root, agent_name, "nonexistent.md")


def test_apply_dry_run_doesnt_skip_history(agent_with_data, tmp_path):
    """Even in dry-run, decisions land in history (so subsequent runs see them)."""
    agents_root, agent_name, today = agent_with_data
    proposals = [EditProposal(
        proposal_id=f"{agent_name}-001", target_agent=agent_name,
        target_file="x", target_section="x", edit_type="addition",
        confidence="high", reversibility="high",
        pattern_summary="x", proposed_diff="d", rationale="r",
        risks="r", verification_plan="v",
        operator_decision="accepted",
    )]
    body = render_report(proposals, agent_name, today, 60)
    reports_dir = agents_root / agent_name / "evals" / "tuning_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / "test_report.md"
    report_path.write_text(body)

    summary = apply_proposals(agents_root, agent_name, "test_report.md", dry_run=True)
    assert summary["dry_run"] is True
    history = agents_root / agent_name / "evals" / "tuning_history.jsonl"
    assert history.exists()
    rec = json.loads(history.read_text().strip())
    assert rec["dry_run"] is True


# ──────────────────────────────────────────────────────────────────
# Runner error handling

def test_runner_missing_agent_raises(tmp_path):
    agents_root = tmp_path / "agents"
    with pytest.raises(AtomicAgentsError, match="not found"):
        TuningRunner(agents_root, "nonexistent")


def test_detector_crash_doesnt_kill_analysis(agent_with_data, monkeypatch):
    """If one detector raises, others still run."""
    agents_root, agent_name, today = agent_with_data
    runner = TuningRunner(agents_root, agent_name, today=today)

    class CrashingDetector(RecurringPersonaFidelityLow):
        def detect(self, ctx):
            raise RuntimeError("simulated crash")

    # Should not raise — should just skip the bad detector
    proposals = runner.analyze(
        window_days=30,
        detectors=[CrashingDetector, HardFailRecurring, StaleNoteRecurring,
                   PromotableMemoryDetected],
    )
    # Other detectors still produce findings
    # (stale note + promotable memory in fixture)
    assert len(proposals) >= 1


# ──────────────────────────────────────────────────────────────────
# LLM polish

def test_polish_proposal_uses_llm_call_when_target_exists(agent_with_data, monkeypatch):
    agents_root, agent_name, today = agent_with_data
    runner = TuningRunner(agents_root, agent_name, today=today)

    # Create a target file so polish has content to read
    soul_path = runner.agent_root / "persona" / "SOUL.md"
    soul_path.parent.mkdir(parents=True, exist_ok=True)
    soul_path.write_text("# SOUL\n\n## Voice\n\nDirect.")

    proposal = EditProposal(
        proposal_id="x", target_agent=agent_name,
        target_file="persona/SOUL.md", target_section="Voice",
        edit_type="addition", confidence="high", reversibility="high",
        pattern_summary="p", proposed_diff="+ original wording\n",
        rationale="r", risks="r", verification_plan="v",
    )

    mock_response = MagicMock(text="polished wording")
    with patch("atomic_agents.tuning._llm.call_llm", return_value=mock_response):
        polished = polish_proposal_text(proposal, runner.agent_root)
    assert "polished wording" in polished.proposed_diff


def test_polish_falls_back_silently_on_failure(agent_with_data):
    agents_root, agent_name, today = agent_with_data
    runner = TuningRunner(agents_root, agent_name, today=today)

    proposal = EditProposal(
        proposal_id="x", target_agent=agent_name,
        target_file="persona/SOUL.md", target_section="Voice",
        edit_type="addition", confidence="high", reversibility="high",
        pattern_summary="p", proposed_diff="+ original\n",
        rationale="r", risks="r", verification_plan="v",
    )

    with patch("atomic_agents.tuning._llm.call_llm", side_effect=Exception("nope")):
        polished = polish_proposal_text(proposal, runner.agent_root)
    # Should fall back to the original diff, no exception
    assert "original" in polished.proposed_diff


# ──────────────────────────────────────────────────────────────────
# P1 regression: --apply must actually write approved proposals
# (Codex finding: summary["applied"] was always zero)

def test_diff_is_auto_applicable_with_additions():
    diff = " ## Voice\n [existing]\n+- New rule here.\n"
    ok, reason = _diff_is_auto_applicable(diff)
    assert ok, reason


def test_diff_is_auto_applicable_rejects_empty():
    ok, reason = _diff_is_auto_applicable("")
    assert not ok
    assert "empty" in reason


def test_diff_is_auto_applicable_rejects_no_additions():
    diff = " context line only\n - removed line\n"
    ok, reason = _diff_is_auto_applicable(diff)
    assert not ok
    assert "addition" in reason


def test_diff_is_auto_applicable_rejects_instructional():
    # Diff that is mostly comments (instructional / multi-step)
    diff = (
        "# Step 1: edit persona/SOUL.md\n"
        "# Step 2: mark note archived\n"
        "# Step 3: update INDEX.md\n"
        "+  superseded_by: persona/SOUL.md\n"
    )
    ok, reason = _diff_is_auto_applicable(diff)
    assert not ok
    assert "manual" in reason or "instructional" in reason


def test_apply_diff_appends_when_no_context(tmp_path):
    target = tmp_path / "SOUL.md"
    target.write_text("## Voice\n\nDirect.\n")
    diff = "+- Never open with hedge language.\n"
    result = _apply_diff_to_file(target, diff)
    assert "Never open with hedge language." in result
    assert "Direct." in result  # original preserved


def test_apply_diff_inserts_after_context(tmp_path):
    target = tmp_path / "SOUL.md"
    target.write_text("## Voice\n\nExisting rule.\n\n## Other\n\nFoo.\n")
    diff = " Existing rule.\n+- New rule after existing.\n"
    result = _apply_diff_to_file(target, diff)
    lines = result.splitlines()
    existing_idx = next(i for i, l in enumerate(lines) if "Existing rule." in l)
    new_idx = next(i for i, l in enumerate(lines) if "New rule after existing." in l)
    assert new_idx == existing_idx + 1


def test_apply_diff_handles_missing_context_gracefully(tmp_path):
    """Context line not found — additions should still be appended at end."""
    target = tmp_path / "SOUL.md"
    target.write_text("## Voice\n\nSomething else.\n")
    diff = " Context that doesn't exist\n+- Addition.\n"
    result = _apply_diff_to_file(target, diff)
    assert "Addition." in result
    assert "Something else." in result  # original still present


def test_apply_diff_on_new_file(tmp_path):
    """Target file doesn't exist yet — additions create it."""
    target = tmp_path / "nonexistent.md"
    diff = "+- First line.\n+- Second line.\n"
    result = _apply_diff_to_file(target, diff)
    assert "First line." in result
    assert "Second line." in result


def _make_report_with_proposals(reports_dir, agent_name, today, proposals):
    """Helper: write a tuning report and return its filename."""
    body = render_report(proposals, agent_name, today, 60)
    report_path = reports_dir / f"{today.isoformat()}_proposal.md"
    report_path.write_text(body)
    return report_path.name


def test_apply_writes_accepted_proposal_to_file(agent_with_data):
    """Core regression: apply_proposals with an accepted proposal must write the file."""
    agents_root, agent_name, today = agent_with_data
    agent_root = agents_root / agent_name

    # Create a target file that the proposal will edit
    soul_path = agent_root / "persona" / "SOUL.md"
    soul_path.parent.mkdir(parents=True, exist_ok=True)
    soul_path.write_text("## Voice\n\nDirect.\n")

    proposal = EditProposal(
        proposal_id=f"{agent_name}-{today.isoformat()}-001",
        target_agent=agent_name,
        target_file="persona/SOUL.md",
        target_section="Voice",
        edit_type="addition",
        confidence="high",
        reversibility="high",
        pattern_summary="hedge language pattern",
        proposed_diff=" Direct.\n+- Never open with hedge language.\n",
        rationale="r",
        risks="r",
        verification_plan="v",
        operator_decision="accepted",
    )

    reports_dir = agent_root / "evals" / "tuning_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_name = _make_report_with_proposals(reports_dir, agent_name, today, [proposal])

    summary = apply_proposals(agents_root, agent_name, report_name)

    # The key regression: applied must be > 0
    assert summary["applied"] == 1, f"applied={summary['applied']}, skipped={summary['skipped_ids']}, failed={summary['failed_ids']}"
    assert proposal.proposal_id in summary["applied_ids"]

    # The file must actually be modified
    new_content = soul_path.read_text()
    assert "Never open with hedge language." in new_content
    assert "Direct." in new_content  # original preserved


def test_apply_does_not_write_on_dry_run(agent_with_data):
    """dry_run=True must not modify any persona/memory files."""
    agents_root, agent_name, today = agent_with_data
    agent_root = agents_root / agent_name

    soul_path = agent_root / "persona" / "SOUL.md"
    soul_path.parent.mkdir(parents=True, exist_ok=True)
    original_content = "## Voice\n\nOriginal.\n"
    soul_path.write_text(original_content)

    proposal = EditProposal(
        proposal_id=f"{agent_name}-001",
        target_agent=agent_name,
        target_file="persona/SOUL.md",
        target_section="Voice",
        edit_type="addition",
        confidence="high",
        reversibility="high",
        pattern_summary="p",
        proposed_diff=" Original.\n+- New rule.\n",
        rationale="r",
        risks="r",
        verification_plan="v",
        operator_decision="accepted",
    )

    reports_dir = agent_root / "evals" / "tuning_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_name = _make_report_with_proposals(reports_dir, agent_name, today, [proposal])

    summary = apply_proposals(agents_root, agent_name, report_name, dry_run=True)

    assert summary["dry_run"] is True
    # File must NOT be modified
    assert soul_path.read_text() == original_content
    # History should still be written (dry_run records decisions)
    history = agents_root / agent_name / "evals" / "tuning_history.jsonl"
    assert history.exists()


def test_apply_respects_write_path_enforcement(agent_with_data):
    """Proposals targeting files outside tools.md write_paths must be blocked."""
    agents_root, agent_name, today = agent_with_data
    agent_root = agents_root / agent_name

    # Write a tools.md that only allows writes to persona/
    tools_path = agent_root / "tools.md"
    persona_path = agent_root / "persona"
    persona_path.mkdir(parents=True, exist_ok=True)
    tools_path.write_text(
        f"## Write paths\n\n- `{persona_path}`\n"
    )

    # Proposal targets memory/ which is NOT in write_paths
    proposal = EditProposal(
        proposal_id=f"{agent_name}-001",
        target_agent=agent_name,
        target_file="memory/some_note.md",
        target_section="frontmatter",
        edit_type="modification",
        confidence="high",
        reversibility="high",
        pattern_summary="p",
        proposed_diff="+pinned: true\n",
        rationale="r",
        risks="r",
        verification_plan="v",
        operator_decision="accepted",
    )

    reports_dir = agent_root / "evals" / "tuning_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_name = _make_report_with_proposals(reports_dir, agent_name, today, [proposal])

    summary = apply_proposals(agents_root, agent_name, report_name)

    # Should be skipped (write_path violation), not applied
    assert summary["applied"] == 0
    assert summary["skipped"] == 1
    assert proposal.proposal_id in summary["skipped_ids"]

    # History should still record the decision
    history = agents_root / agent_name / "evals" / "tuning_history.jsonl"
    rec = json.loads(history.read_text().strip())
    assert rec["applied"] is False
    assert "write_path" in (rec.get("skip_reason") or "")


def test_apply_skips_instructional_diffs(agent_with_data):
    """Proposals with instructional (comment-heavy) diffs must be marked manual."""
    agents_root, agent_name, today = agent_with_data
    agent_root = agents_root / agent_name

    proposal = EditProposal(
        proposal_id=f"{agent_name}-001",
        target_agent=agent_name,
        target_file="persona/SOUL.md",
        target_section="Voice",
        edit_type="addition",
        confidence="medium",
        reversibility="medium",
        pattern_summary="p",
        proposed_diff=(
            "# Step 1: append note body to persona/SOUL.md\n"
            "# Step 2: mark note archived\n"
            "# Step 3: update INDEX.md\n"
            "+  superseded_by: persona/SOUL.md\n"
        ),
        rationale="r",
        risks="r",
        verification_plan="v",
        operator_decision="accepted",
    )

    reports_dir = agent_root / "evals" / "tuning_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_name = _make_report_with_proposals(reports_dir, agent_name, today, [proposal])

    summary = apply_proposals(agents_root, agent_name, report_name)

    assert summary["applied"] == 0
    assert summary["skipped"] == 1
    assert proposal.proposal_id in summary["skipped_ids"]


def test_parse_report_proposals_extracts_diff(agent_with_data, tmp_path):
    """parse_report_proposals must recover proposed_diff from the report body."""
    agents_root, agent_name, today = agent_with_data
    agent_root = agents_root / agent_name

    proposal = EditProposal(
        proposal_id=f"{agent_name}-diff-001",
        target_agent=agent_name,
        target_file="persona/SOUL.md",
        target_section="Voice",
        edit_type="addition",
        confidence="high",
        reversibility="high",
        pattern_summary="p",
        proposed_diff=" Direct.\n+- Never hedge.\n",
        rationale="r",
        risks="r",
        verification_plan="v",
        operator_decision="accepted",
    )

    body = render_report([proposal], agent_name, today, 60)
    report_path = tmp_path / "report.md"
    report_path.write_text(body)

    parsed = parse_report_proposals(report_path)
    assert len(parsed) == 1
    assert "Never hedge." in parsed[0].proposed_diff


def test_apply_history_records_applied_true_when_written(agent_with_data):
    """tuning_history.jsonl must record applied=True when a file was actually written."""
    agents_root, agent_name, today = agent_with_data
    agent_root = agents_root / agent_name

    soul_path = agent_root / "persona" / "SOUL.md"
    soul_path.parent.mkdir(parents=True, exist_ok=True)
    soul_path.write_text("## Voice\n\nDirect.\n")

    proposal = EditProposal(
        proposal_id=f"{agent_name}-{today.isoformat()}-001",
        target_agent=agent_name,
        target_file="persona/SOUL.md",
        target_section="Voice",
        edit_type="addition",
        confidence="high",
        reversibility="high",
        pattern_summary="hedge language",
        proposed_diff=" Direct.\n+- No hedge openers.\n",
        rationale="r",
        risks="r",
        verification_plan="v",
        operator_decision="accepted",
    )

    reports_dir = agent_root / "evals" / "tuning_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_name = _make_report_with_proposals(reports_dir, agent_name, today, [proposal])
    apply_proposals(agents_root, agent_name, report_name)

    history_path = agents_root / agent_name / "evals" / "tuning_history.jsonl"
    rec = json.loads(history_path.read_text().strip())
    assert rec["applied"] is True
    assert rec["diff_applied"] is not None

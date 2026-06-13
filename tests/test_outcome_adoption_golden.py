"""Golden tests for OutcomeBackend write-path adoption (#448 PR2).

These tests prove that OutcomeRunner.run() ACTUALLY CALLS self.outcome_backend.write_result()
after the routing change in #448 PR2. TEST 30 in test_outcome_backend_conformance.py pins
the serializers equal in isolation — it does NOT verify that run() calls write_result.
These golden tests close that gap.

Four cases covered:
  GOLDEN 1 — Default output_dir: result.json lands at the canonical path
              (agent_root/outcomes/runs/<run_id>/result.json), bytes match
              the pre-adoption _write_result_json serializer, write_result spy
              was called exactly once with (agent_name, run_id, result).

  GOLDEN 2 — Custom output_dir: result.json lands at the CANONICAL path
              (agent_root/outcomes/runs/<run_id>/result.json), NOT in
              custom_dir. Artifact files still go to custom_dir (unchanged).
              This is the conscious A1 correctness fix: custom-output_dir
              result.json was previously invisible to list_runs/read_result
              (orphan bug); the relocation fixes it. DEFAULT case is
              byte-identical-location.

  GOLDEN 3 — kwarg-wins-over-env: ATOMIC_AGENTS_OUTCOME_BACKEND env var is set
              to 'filesystem' (which WOULD produce a different backend instance)
              but the outcome_backend= kwarg overrides it and the injected spy
              is the backend used for write_result.

  GOLDEN 4 — write_result error propagates: if the backend's write_result raises
              AtomicAgentsError, run() does NOT swallow it — the error propagates
              to the caller. Pins spec/42 MUST 9 + Principle #5 (audit trail is
              structural; silent swallow violates it).

Design:
  - Each golden drives a REAL `runner.run()` with `AtomicAgent` + the judge
    LLM mocked, so it proves run() actually reaches and CALLS write_result.
  - Byte-identity is asserted by re-feeding the run-produced result back
    through the pre-adoption `_write_result_json` reference serializer and
    comparing on-disk bytes (TEST 30's serializer, driven via the runner).
  - OutcomeRunner has NO injectable clock (verified: datetime.now() called
    directly for run_id/timestamps). The run mints its own run_id; we assert
    against `actual_result`, never a hardcoded run_id, so non-deterministic
    timestamps don't matter — the byte comparison is against the same result.
  - Spy on outcome_backend.write_result (MagicMock wraps= real backend) so
    the assertion that run() CALLS write_result is not vacuous.
  - Do NOT add a clock kwarg to OutcomeRunner (out of scope; filed as
    follow-up if full-run determinism is wanted).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atomic_agents.exceptions import AtomicAgentsError
from atomic_agents.outcome import OutcomeRunner
from atomic_agents.outcome.filesystem import FilesystemOutcomeBackend
from atomic_agents.types import CostCheckResult


# ──────────────────────────────────────────────────────────────────
# Helpers


def _make_agent_vault(tmp_path: Path) -> tuple[Path, str]:
    """Minimal agent folder required by OutcomeRunner + AtomicAgent construction."""
    agents_root = tmp_path / "agents"
    agent_root = agents_root / "testagent"
    persona = agent_root / "persona"
    persona.mkdir(parents=True)
    (persona / "IDENTITY.md").write_text("# IDENTITY\n\nI am a test agent.")
    (persona / "SOUL.md").write_text("# SOUL\n\nBrief.")
    (agent_root / "tools.md").write_text(
        "# TOOLS\n\n## Read paths\n- "
        + str(agent_root)
        + "\n\n## Write paths\n- "
        + str(agent_root)
        + "\n"
    )
    (agent_root / "model.md").write_text(
        "# MODEL\n\n## Default model\n\nclaude-sonnet-4-6-20260101\n\n"
        "## Fallback\n\nclaude-haiku-4-5-20251001\n"
    )
    return agents_root, "testagent"


# ──────────────────────────────────────────────────────────────────
# GOLDEN 1 — Default output_dir: spy proves run() calls write_result;
#            bytes match pre-adoption _write_result_json serializer;
#            result.json lands at canonical path.


def test_golden_routing_default_output_dir(tmp_path: Path) -> None:
    """Proves run() CALLS write_result (the routing change TEST 30 does not cover).

    Verifies:
      (a) outcome_backend.write_result is called exactly once
      (b) it is called with (agent_name, run_id, result) — the correct arguments
      (c) the bytes on disk match what _write_result_json produces (zero behavior change)
      (d) result.json lands at the canonical path (agent_root/outcomes/runs/<run_id>/result.json)
    """
    agents_root, agent_name = _make_agent_vault(tmp_path)
    agent_root = agents_root / agent_name

    # NOTE: GOLDEN 1 drives a REAL run() (which mints its OWN run_id and builds its
    # OWN OutcomeResult) and asserts against that result (actual_result). Byte-identity
    # is proven by re-feeding actual_result through _write_result_json (TEST 30's
    # serializer) and comparing on-disk bytes — see assertion (c) below.

    # Real backend used as the write target
    real_backend = FilesystemOutcomeBackend(agents_root, agent_name)
    # Spy wraps the real backend so write_result actually writes to disk
    # while we can assert it was called
    spy = MagicMock(wraps=real_backend)

    runner = OutcomeRunner(
        agents_root=agents_root,
        agent_name=agent_name,
        judge_model="gpt-5",
        outcome_backend=spy,
    )

    # Drive the write SEAM: mock AtomicAgent + LLM so run() proceeds
    # and reaches the write site, then drive a satisfied outcome.
    satisfied_verdict = json.dumps(
        {
            "satisfied": True,
            "criterion_results": [{"criterion": "completeness", "met": True}],
            "explanation": "All criteria met.",
            "rubric_contradicts_description": False,
        }
    )

    agent_resp = MagicMock()
    agent_resp.text = "Here is the artifact."
    agent_resp.model = "claude-sonnet-4-6-20260101"
    agent_resp.input_tokens = 100
    agent_resp.output_tokens = 50
    agent_resp.cost_usd = 0.001
    agent_resp.skipped = False
    agent_resp.skip_reason = ""

    judge_resp = MagicMock()
    judge_resp.text = satisfied_verdict
    judge_resp.input_tokens = 80
    judge_resp.output_tokens = 30

    with patch("atomic_agents.outcome.AtomicAgent") as MockAgent:
        mock_instance = MagicMock()
        mock_instance.call.return_value = agent_resp
        mock_instance.config.default_model = "claude-sonnet-4-6-20260101"
        mock_instance._check_cost_guardrails.return_value = CostCheckResult(allow=True)
        MockAgent.return_value = mock_instance

        with patch("atomic_agents.outcome._llm.call_llm", return_value=judge_resp):
            actual_result = runner.run(
                description="Write a test document.",
                rubric="## completeness\nThe output covers all required sections.",
                max_iterations=1,
            )

    # (a) write_result was called exactly once
    assert spy.write_result.call_count == 1, (
        "run() MUST call self.outcome_backend.write_result exactly once; "
        f"got {spy.write_result.call_count} calls. The routing change is broken."
    )

    # (b) called with (agent_name, run_id, result)
    call_args = spy.write_result.call_args
    called_agent_id, called_run_id, called_result = call_args[0]
    assert called_agent_id == agent_name, (
        f"write_result called with agent_id={called_agent_id!r}, expected {agent_name!r}"
    )
    assert called_run_id == actual_result.run_id, (
        f"write_result called with run_id={called_run_id!r}, expected {actual_result.run_id!r}"
    )
    assert called_result is actual_result, (
        "write_result must be called with the SAME result object returned by run()"
    )

    # (c) result.json bytes match _write_result_json reference serializer
    canonical_path = (
        agent_root / "outcomes" / "runs" / actual_result.run_id / "result.json"
    )
    assert canonical_path.exists(), (
        f"result.json MUST exist at canonical path {canonical_path}"
    )

    reference_dir = tmp_path / "reference_run"
    reference_dir.mkdir(parents=True, exist_ok=True)
    # Drive the pre-adoption serializer directly (TEST 30's shape)
    runner._write_result_json(reference_dir, actual_result)
    expected_bytes = (reference_dir / "result.json").read_bytes()
    actual_bytes = canonical_path.read_bytes()

    assert actual_bytes == expected_bytes, (
        "result.json bytes written via write_result MUST be byte-identical to "
        "_write_result_json (zero on-disk behavior change). The serializers have diverged."
    )

    # (d) result.json is at the canonical path (not scattered)
    runs_dir = agent_root / "outcomes" / "runs"
    all_result_files = list(runs_dir.glob("*/result.json"))
    assert len(all_result_files) == 1, (
        f"Expected exactly 1 result.json in runs/; found: {all_result_files}"
    )
    assert all_result_files[0] == canonical_path, (
        f"result.json is at {all_result_files[0]}, expected canonical {canonical_path}"
    )


# ──────────────────────────────────────────────────────────────────
# GOLDEN 2 — Custom output_dir: result.json relocates to CANONICAL path
#            (A1 conscious correctness fix); artifacts stay in custom dir.


def test_golden_routing_custom_output_dir(tmp_path: Path) -> None:
    """Custom --output-dir: result.json lands at CANONICAL path, artifacts at custom dir.

    This is the A1 conscious correctness fix (spec/42 §Shipping history PR 2):
    the custom-output_dir result.json was previously invisible to list_runs /
    read_result / export (orphan bug). After the routing change, result.json always
    lands at agent_root/outcomes/runs/<run_id>/result.json regardless of output_dir.
    Artifact FILES still go to output_dir — only the audit envelope relocates.

    Default case (output_dir == canonical) is byte-identical-location (GOLDEN 1).
    """
    agents_root, agent_name = _make_agent_vault(tmp_path)
    agent_root = agents_root / agent_name

    custom_dir = tmp_path / "my_custom_outputs"
    custom_dir.mkdir(parents=True, exist_ok=True)

    real_backend = FilesystemOutcomeBackend(agents_root, agent_name)
    spy = MagicMock(wraps=real_backend)

    runner = OutcomeRunner(
        agents_root=agents_root,
        agent_name=agent_name,
        judge_model="gpt-5",
        outcome_backend=spy,
    )

    satisfied_verdict = json.dumps(
        {
            "satisfied": True,
            "criterion_results": [],
            "explanation": "Done.",
            "rubric_contradicts_description": False,
        }
    )

    agent_resp = MagicMock()
    agent_resp.text = "Output written."
    agent_resp.model = "claude-sonnet-4-6-20260101"
    agent_resp.input_tokens = 50
    agent_resp.output_tokens = 25
    agent_resp.cost_usd = 0.0005
    agent_resp.skipped = False
    agent_resp.skip_reason = ""

    judge_resp = MagicMock()
    judge_resp.text = satisfied_verdict
    judge_resp.input_tokens = 40
    judge_resp.output_tokens = 20

    def write_artifact_side_effect(work_item, **kwargs):
        # Simulate agent writing a file to custom_dir
        artifact = custom_dir / "output.txt"
        artifact.write_text("Artifact content", encoding="utf-8")
        return agent_resp

    with patch("atomic_agents.outcome.AtomicAgent") as MockAgent:
        mock_instance = MagicMock()
        mock_instance.call.side_effect = write_artifact_side_effect
        mock_instance.config.default_model = "claude-sonnet-4-6-20260101"
        mock_instance._check_cost_guardrails.return_value = CostCheckResult(allow=True)
        MockAgent.return_value = mock_instance

        with patch("atomic_agents.outcome._llm.call_llm", return_value=judge_resp):
            actual_result = runner.run(
                description="Write output to custom dir.",
                rubric="## completeness\nMust be complete.",
                max_iterations=1,
                output_dir=custom_dir,
            )

    # write_result was called
    assert spy.write_result.call_count == 1, (
        f"run() MUST call write_result exactly once; got {spy.write_result.call_count}"
    )

    # result.json lands at CANONICAL path (NOT in custom_dir)
    canonical_path = (
        agent_root / "outcomes" / "runs" / actual_result.run_id / "result.json"
    )
    assert canonical_path.exists(), (
        f"result.json MUST exist at canonical path {canonical_path}. "
        "A1 ruling: the audit envelope always goes to outcomes/runs/<run_id>/result.json."
    )

    # result.json does NOT exist in custom_dir (relocation confirmed)
    custom_result_json = custom_dir / "result.json"
    assert not custom_result_json.exists(), (
        f"result.json MUST NOT exist in custom_dir {custom_dir} after the A1 fix. "
        "Only ARTIFACT files go to custom_dir; the audit envelope goes to canonical path."
    )

    # custom_dir still exists and artifacts actually landed there
    assert custom_dir.exists(), "custom_dir must still exist for artifact writes"
    assert (custom_dir / "output.txt").exists(), (
        "Artifact file output.txt MUST exist in custom_dir — "
        "artifacts stay in custom_dir while only the audit envelope (result.json) "
        "relocates to the canonical path. This pins the 'artifacts stay, envelope "
        "relocates' invariant — not just the relocation half."
    )

    # run_id visible to list_runs (no longer an orphan)
    run_ids = real_backend.list_runs(agent_name)
    assert actual_result.run_id in run_ids, (
        f"run_id {actual_result.run_id!r} must be visible to list_runs after A1 fix. "
        "Before the fix, custom-output_dir result.json was invisible (orphan bug)."
    )


# ──────────────────────────────────────────────────────────────────
# GOLDEN 3 — outcome_backend kwarg wiring: injected backend is used,
#            not the env-var default.


def test_golden_outcome_backend_kwarg_wins_over_default(
    tmp_path: Path, monkeypatch
) -> None:
    """OutcomeRunner outcome_backend= kwarg wins over ATOMIC_AGENTS_OUTCOME_BACKEND env var.

    Confirms the kwarg-wins-over-env pattern (mirrors PR1's goal_backend):
    even when ATOMIC_AGENTS_OUTCOME_BACKEND points to 'filesystem' (which WOULD
    cause the factory to produce a new, independent backend instance), the kwarg-
    injected spy is the backend that gets stored and used for write_result.

    This proves the precedence — not just "kwarg is stored" but "kwarg beats an
    env-var that would otherwise change behavior".
    """
    agents_root, agent_name = _make_agent_vault(tmp_path)
    agent_root = agents_root / agent_name

    injected_backend = FilesystemOutcomeBackend(agents_root, agent_name)
    spy = MagicMock(wraps=injected_backend)

    # Set the env var to 'filesystem' so that the factory WOULD produce a
    # different (unwrapped) FilesystemOutcomeBackend instance if kwarg is ignored.
    # The kwarg must win — spy (not a fresh factory instance) must be used.
    monkeypatch.setenv("ATOMIC_AGENTS_OUTCOME_BACKEND", "filesystem")

    runner = OutcomeRunner(
        agents_root=agents_root,
        agent_name=agent_name,
        judge_model="gpt-5",
        outcome_backend=spy,
    )

    # Verify the runner stores the injected spy, not a factory-created instance
    assert runner.outcome_backend is spy, (
        "runner.outcome_backend MUST be the kwarg-injected spy backend, not a factory-created "
        "one. The env var ATOMIC_AGENTS_OUTCOME_BACKEND='filesystem' was set, so if kwarg is "
        "ignored the runner would hold a different FilesystemOutcomeBackend instance."
    )

    satisfied_verdict = json.dumps(
        {
            "satisfied": True,
            "criterion_results": [],
            "explanation": "Done.",
            "rubric_contradicts_description": False,
        }
    )

    agent_resp = MagicMock()
    agent_resp.text = "Done."
    agent_resp.model = "claude-sonnet-4-6-20260101"
    agent_resp.input_tokens = 50
    agent_resp.output_tokens = 20
    agent_resp.cost_usd = 0.0004
    agent_resp.skipped = False
    agent_resp.skip_reason = ""

    judge_resp = MagicMock()
    judge_resp.text = satisfied_verdict
    judge_resp.input_tokens = 30
    judge_resp.output_tokens = 15

    with patch("atomic_agents.outcome.AtomicAgent") as MockAgent:
        mock_instance = MagicMock()
        mock_instance.call.return_value = agent_resp
        mock_instance.config.default_model = "claude-sonnet-4-6-20260101"
        mock_instance._check_cost_guardrails.return_value = CostCheckResult(allow=True)
        MockAgent.return_value = mock_instance

        with patch("atomic_agents.outcome._llm.call_llm", return_value=judge_resp):
            result = runner.run(
                description="Kwarg test.",
                rubric="## simple\nMust pass.",
                max_iterations=1,
            )

    # The SPY backend was used for the write
    assert spy.write_result.call_count == 1, (
        "The injected kwarg backend's write_result MUST be called, "
        "not a factory-created backend's"
    )

    # Result is readable via the injected backend
    canonical_path = agent_root / "outcomes" / "runs" / result.run_id / "result.json"
    assert canonical_path.exists(), (
        f"result.json must exist at canonical path {canonical_path} after write "
        "via injected backend"
    )


# ──────────────────────────────────────────────────────────────────
# GOLDEN 4 — write_result error propagates: run() does NOT swallow
#            AtomicAgentsError raised by backend.write_result.


def test_golden_write_result_error_propagates(tmp_path: Path) -> None:
    """run() does NOT swallow AtomicAgentsError raised by write_result.

    Pins the no-catch error-propagation discipline: the implementation
    deliberately does NOT catch write_result errors (spec/42 MUST 9 +
    Principle #5 — audit trail is structural; silently swallowing a write
    failure would leave the caller unaware the result was never persisted).

    Injects a backend whose write_result raises AtomicAgentsError and drives
    run() through the full mocked-agent/judge path, asserting the exception
    surfaces at the call site.
    """
    agents_root, agent_name = _make_agent_vault(tmp_path)

    # Backend whose write_result raises AtomicAgentsError unconditionally
    error_backend = MagicMock(spec=FilesystemOutcomeBackend)
    error_backend.write_result.side_effect = AtomicAgentsError(
        "write_result: simulated write failure (GOLDEN 4)"
    )

    runner = OutcomeRunner(
        agents_root=agents_root,
        agent_name=agent_name,
        judge_model="gpt-5",
        outcome_backend=error_backend,
    )

    satisfied_verdict = json.dumps(
        {
            "satisfied": True,
            "criterion_results": [],
            "explanation": "Done.",
            "rubric_contradicts_description": False,
        }
    )

    agent_resp = MagicMock()
    agent_resp.text = "Output."
    agent_resp.model = "claude-sonnet-4-6-20260101"
    agent_resp.input_tokens = 40
    agent_resp.output_tokens = 15
    agent_resp.cost_usd = 0.0003
    agent_resp.skipped = False
    agent_resp.skip_reason = ""

    judge_resp = MagicMock()
    judge_resp.text = satisfied_verdict
    judge_resp.input_tokens = 30
    judge_resp.output_tokens = 12

    with patch("atomic_agents.outcome.AtomicAgent") as MockAgent:
        mock_instance = MagicMock()
        mock_instance.call.return_value = agent_resp
        mock_instance.config.default_model = "claude-sonnet-4-6-20260101"
        mock_instance._check_cost_guardrails.return_value = CostCheckResult(allow=True)
        MockAgent.return_value = mock_instance

        with patch("atomic_agents.outcome._llm.call_llm", return_value=judge_resp):
            with pytest.raises(AtomicAgentsError, match="simulated write failure"):
                runner.run(
                    description="Error propagation test.",
                    rubric="## simple\nMust pass.",
                    max_iterations=1,
                )

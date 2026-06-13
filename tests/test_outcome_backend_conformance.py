"""Conformance tests for OutcomeBackend Protocol (spec/42).

44 test cases (46 collected after fixture parametrization) covering the
OutcomeBackend Implementer Contract (the protocol-behavior subset is
parametrized over every registered backend via the ``backend`` /
``backend_with_result`` fixtures; see PARAMETRIZATION below).

NOTE ON NUMBERING: the "TEST N" labels below are this file's own granular
test-case indices, NOT spec/42 MUST numbers (spec/42 has exactly 9 MUSTs).
Each test case maps to its governing spec/42 MUST in the trailing parens so a
contributor reconciling code-to-spec lands on the right requirement.

  TEST 1  — side-effect-free construction (spec/42 MUST 1)
  TEST 2  — capability honesty (spec/42 MUST 2)
  TEST 3  — list_runs() returns [] for absent outcomes/ dir (spec/42 MUST 3)
  TEST 4  — write_result() + read_result() round-trip (spec/42 MUST 4/5)
  TEST 5  — read_result() raises AtomicAgentsError when absent (spec/42 MUST 7)
  TEST 6  — read_result() raises OutcomeCorrupted on corrupt JSON (spec/42 MUST 6)
  TEST 7  — read_result() raises OutcomeCorrupted on missing required fields (spec/42 MUST 6)
  TEST 8  — write_result() write-once: second write raises (spec/42 MUST 9)
  TEST 9  — list_runs() returns sorted run_ids (spec/42 MUST 3)
  TEST 10 — export() returns OutcomeExport type (spec/42 export contract)
  TEST 11 — export() returns empty OutcomeExport when no runs (spec/42 export contract)
  TEST 12 — export() all equals export(None) (spec/42 export contract)
  TEST 13 — export() artifact_refs are relative-to-agent_root (spec/42 §Artifact-reference portability)
  TEST 14 — export() artifact outside agent_root stays absolute (spec/42 §Artifact-reference portability fallback)
  TEST 15 — export() result_json_bytes is byte-identical to written file (spec/42 MUST 9/Tier B)
  TEST 16 — OutcomeResult/IterationRecord are mutable (spec/42 mutable-dataclass note)
  TEST 17 — OutcomeCapabilities is frozen (spec/42 MUST 2)
  TEST 18 — OutcomeCapabilities(backend_id='x') valid with all defaults False (spec/42 MUST 2)
  TEST 19 — backend_id stability (spec/42 MUST 2)
  TEST 20 — OutcomeCapabilities field types are bool (spec/42 MUST 2)
  TEST 21 — supports_canonical_export=True for filesystem (spec/42 MUST 2 / spec/40)
  TEST 22 — supports_artifact_storage=True for filesystem (spec/42 MUST 2)
  TEST 23 — env var dispatches registered custom backend (spec/42 MUST 2 / registry)
  TEST 24 — get_outcome_backend() raises BackendNotRegistered for unknown id
  TEST 25 — _redact_for_error_message() URL redaction
  TEST 26 — _redact_for_error_message() DSN redaction
  TEST 27 — _redact_for_error_message() truncation
  TEST 28 — _redact_for_error_message() passthrough for short value
  TEST 29 — get_default_outcome_backend() empty-string env var uses filesystem
  TEST 30 — Golden-file: write_result() byte-identical to old runner output (spec/42 Tier B)
  TEST 31 — from_dict() / to_dict() round-trip with Path fields (spec/42 types contract)
  TEST 32 — OutcomeResult.from_dict() coerces output_files to list[Path]
  TEST 33 — IterationRecord.from_dict() coerces artifact_path to Path
  TEST 34 — doctor.check_outcome_backend returns PASS for empty agent
  TEST 35 — doctor.check_outcome_backend returns PASS with runs + dual-probe
  TEST 36 — doctor.check_outcome_backend returns FAIL for bad env var
  TEST 37 — export() corrupt-but-present JSON: bytes preserved, refs empty
  TEST 38 — export() resolve-both-sides symlink rebases to relative (artifact-reference portability)
  TEST 39 — get_default_outcome_backend() unknown env var raises BackendNotRegistered
  TEST 40 — doctor.check_outcome_backend list_runs() raises → FAIL (light probe)
  TEST 41 — doctor.check_outcome_backend read_result() corrupt → FAIL (heavy probe)
  TEST 42 — doctor.check_outcome_backend vanished-run TOCTOU benign → PASS
  TEST 43 — AtomicAgent.outcome_backend per-agent handle wiring (agent.py)
  TEST 44 — doctor.check_outcome_backend read_result() unexpected error → FAIL

PARAMETRIZATION: protocol-behavior tests use the ``backend`` / ``backend_with_result``
fixtures parametrized over BACKEND_FACTORIES (currently just 'filesystem'). Adding a
second backend to BACKEND_FACTORIES picks up every protocol-behavior test automatically.

Filesystem-specific tests are deliberately NOT parametrized: path-traversal guards,
byte-identity golden tests, ATOMIC_AGENTS_OUTCOME_BACKEND registry dispatch. Pure-
dataclass tests (OutcomeResult/IterationRecord/OutcomeCapabilities) need no backend.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from atomic_agents.outcome.filesystem import FilesystemOutcomeBackend
from atomic_agents.outcome.types import (
    IterationRecord,
    OutcomeCapabilities,
    OutcomeExport,
    OutcomeResult,
)
from atomic_agents.exceptions import (
    AtomicAgentsError,
    OutcomeCorrupted,
    PathTraversalError,
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers


def _make_iteration_record(
    iteration: int = 0,
    artifact_path: Path | None = None,
) -> IterationRecord:
    return IterationRecord(
        iteration=iteration,
        agent_response="Here is my draft.",
        agent_input_tokens=100,
        agent_output_tokens=50,
        agent_cost_usd=0.001,
        agent_latency_ms=500,
        judge_response_raw='{"satisfied": true, "criterion_results": [], "explanation": "Looks good."}',
        judge_verdict={
            "satisfied": True,
            "criterion_results": [],
            "explanation": "Looks good.",
        },
        judge_cost_usd=0.0005,
        judge_input_tokens=80,
        judge_output_tokens=30,
        artifact_path=artifact_path,
        timestamp="2026-06-11T19:00:00+00:00",
    )


def _make_outcome_result(
    run_id: str = "outcome-20260611-190000-abc12345",
    output_files: list[Path] | None = None,
    artifact_path: Path | None = None,
) -> OutcomeResult:
    rec = _make_iteration_record(iteration=0, artifact_path=artifact_path)
    result = OutcomeResult(
        run_id=run_id,
        description="Write a test document.",
        rubric_source="inline",
        max_iterations=3,
        status="satisfied",
        explanation="All criteria met.",
        iterations=[rec],
        final_iteration_idx=0,
        total_cost_usd=0.0015,
        total_input_tokens=180,
        total_output_tokens=80,
        started_at="2026-06-11T19:00:00+00:00",
        ended_at="2026-06-11T19:00:05+00:00",
        output_files=output_files or [],
    )
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Parametrized fixtures (protocol-behavior tests)

# Add alternate backend IDs here when they exist.
BACKEND_FACTORIES = ["filesystem"]


def _make_backend(backend_id: str, tmp_path: Path) -> FilesystemOutcomeBackend:
    if backend_id == "filesystem":
        agents_root = tmp_path / "agents"
        agents_root.mkdir(parents=True, exist_ok=True)
        return FilesystemOutcomeBackend(agents_root, "testagent")
    raise ValueError(f"Unknown backend_id: {backend_id!r}")


@pytest.fixture(params=BACKEND_FACTORIES)
def backend(request, tmp_path: Path):
    return _make_backend(request.param, tmp_path)


@pytest.fixture(params=BACKEND_FACTORIES)
def backend_with_result(request, tmp_path: Path):
    """Backend with one completed run written to it."""
    b = _make_backend(request.param, tmp_path)
    result = _make_outcome_result()
    b.write_result("testagent", result.run_id, result)
    return b, result


# ──────────────────────────────────────────────────────────────────────────────
# TEST 1 — side-effect-free construction (spec/42 MUST 1)


def test_construction_is_side_effect_free(backend) -> None:
    """FilesystemOutcomeBackend construction MUST be side-effect-free.

    No filesystem I/O in __init__ — outcomes/ dir MUST NOT be created at
    construction time. This matches GoalBackend MUST 1.
    """
    agent_root = backend._agent_root
    assert not (agent_root / "outcomes").exists(), (
        "Construction MUST NOT create the outcomes/ directory"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 2 — capability honesty (spec/42 MUST 2)


def test_capabilities_returns_outcome_capabilities(backend) -> None:
    """capabilities() returns an OutcomeCapabilities instance."""
    caps = backend.capabilities()
    assert isinstance(caps, OutcomeCapabilities)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 3 — list_runs() returns [] for absent outcomes/ dir (spec/42 MUST 3)


def test_list_runs_returns_empty_when_absent(backend) -> None:
    """list_runs() MUST return [] when outcomes/runs/ does not exist.

    MUST NOT raise FileNotFoundError. Agents that have never run an outcome
    have no outcomes/ directory.
    """
    run_ids = backend.list_runs("testagent")
    assert run_ids == []


# ──────────────────────────────────────────────────────────────────────────────
# TEST 4 — write_result() + read_result() round-trip (spec/42 MUST 4/5)


def test_write_read_roundtrip(backend) -> None:
    """write_result() + read_result() round-trip preserves all fields."""
    result = _make_outcome_result()
    backend.write_result("testagent", result.run_id, result)
    loaded = backend.read_result("testagent", result.run_id)

    assert loaded.run_id == result.run_id
    assert loaded.description == result.description
    assert loaded.status == result.status
    assert loaded.explanation == result.explanation
    assert loaded.total_cost_usd == result.total_cost_usd
    assert loaded.total_input_tokens == result.total_input_tokens
    assert len(loaded.iterations) == 1
    assert loaded.iterations[0].iteration == 0
    assert loaded.iterations[0].agent_response == result.iterations[0].agent_response


# ──────────────────────────────────────────────────────────────────────────────
# TEST 5 — read_result() raises AtomicAgentsError when absent (spec/42 MUST 7)


def test_read_result_absent_raises_error(backend) -> None:
    """read_result() MUST raise AtomicAgentsError when no result.json exists."""
    with pytest.raises(AtomicAgentsError):
        backend.read_result("testagent", "outcome-nonexistent-run")


# ──────────────────────────────────────────────────────────────────────────────
# TEST 6 — read_result() raises OutcomeCorrupted on corrupt JSON (spec/42 MUST 6)


def test_read_result_corrupt_json_raises_outcome_corrupted(tmp_path: Path) -> None:
    """read_result() MUST raise OutcomeCorrupted when JSON is unparseable."""
    agents_root = tmp_path / "agents"
    backend = FilesystemOutcomeBackend(agents_root, "testagent")
    run_id = "outcome-20260611-corrupt-test"
    run_dir = agents_root / "testagent" / "outcomes" / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "result.json").write_text("NOT VALID JSON {{{{", encoding="utf-8")

    with pytest.raises(OutcomeCorrupted):
        backend.read_result("testagent", run_id)


# TEST 6b — read_result() raises OutcomeCorrupted on valid-JSON-WRONG-SHAPE.
# A JSON list/scalar/null is valid JSON but not an OutcomeResult object; without
# the isinstance(dict) guard, from_dict leaks a raw AttributeError (not the
# spec/42 OutcomeCorrupted the contract promises).


@pytest.mark.parametrize("wrong_shape", ["[1, 2, 3]", '"hello"', "42", "null"])
def test_read_result_wrong_shape_json_raises_outcome_corrupted(
    tmp_path: Path, wrong_shape
) -> None:
    """read_result() MUST raise OutcomeCorrupted for valid JSON that is not an object."""
    agents_root = tmp_path / "agents"
    backend = FilesystemOutcomeBackend(agents_root, "testagent")
    run_id = "outcome-20260611-wrongshape"
    run_dir = agents_root / "testagent" / "outcomes" / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "result.json").write_text(wrong_shape, encoding="utf-8")

    with pytest.raises(OutcomeCorrupted):
        backend.read_result("testagent", run_id)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 7 — read_result() raises OutcomeCorrupted on missing required fields (spec/42 MUST 6)


def test_read_result_missing_fields_raises_outcome_corrupted(tmp_path: Path) -> None:
    """read_result() MUST raise OutcomeCorrupted when required fields are missing."""
    agents_root = tmp_path / "agents"
    backend = FilesystemOutcomeBackend(agents_root, "testagent")
    run_id = "outcome-20260611-missing-fields"
    run_dir = agents_root / "testagent" / "outcomes" / "runs" / run_id
    run_dir.mkdir(parents=True)
    # Write JSON that is valid but missing required OutcomeResult fields
    (run_dir / "result.json").write_text('{"foo": "bar"}', encoding="utf-8")

    with pytest.raises(OutcomeCorrupted):
        backend.read_result("testagent", run_id)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 8 — write_result() write-once: second write raises (spec/42 MUST 9)


def test_write_result_write_once_contract(backend) -> None:
    """write_result() MUST raise AtomicAgentsError on second write for same run_id.

    spec/42 MUST 9: write-once / result-immutability.
    """
    result = _make_outcome_result()
    backend.write_result("testagent", result.run_id, result)

    with pytest.raises(AtomicAgentsError, match="write-once"):
        backend.write_result("testagent", result.run_id, result)


# TEST 8b — run_id path-traversal is refused on the Protocol surface.
# run_id is external input; a crafted value must not escape <agent_root>/outcomes/runs/.
# Guards the framework invariant "Path traversal refused per _io.safe_resolve_under"
# (matches memory/filesystem.py); PathTraversalError subclasses AtomicAgentsError.


@pytest.mark.parametrize(
    "evil_run_id",
    [
        # out-of-root escape (safe_resolve_under)
        "../../../etc/passwd",
        "../../other-agent/outcomes/runs/x",
        "/tmp/escape",
        "..",
        # in-root degenerate ids that break one-dir-per-run (single-segment check):
        # '' / '.' resolve to runs/ itself; 'foo/bar' is nested-and-never-listed.
        "",
        ".",
        "foo/bar",
    ],
)
def test_write_result_refuses_run_id_traversal(backend, tmp_path, evil_run_id) -> None:
    """write_result() MUST reject any run_id that is not a single run-dir name."""
    result = _make_outcome_result()
    with pytest.raises(PathTraversalError):
        backend.write_result("testagent", evil_run_id, result)
    # Nothing escaped, and no sentinel was written to runs/ itself.
    assert not (tmp_path / "etc" / "passwd").exists()
    runs_dir = backend._agent_root / "outcomes" / "runs"
    assert not (runs_dir / "result.json").exists()


def test_read_result_refuses_run_id_traversal(backend) -> None:
    """read_result() MUST reject a traversing run_id rather than read outside root."""
    with pytest.raises(PathTraversalError):
        backend.read_result("testagent", "../../../etc/passwd")


# TEST 8c — list_runs() excludes run dirs that lack a result.json (incomplete runs).


def test_list_runs_excludes_dir_without_result_json(backend) -> None:
    """list_runs() returns only run dirs that actually contain a result.json."""
    r = _make_outcome_result()
    backend.write_result("testagent", r.run_id, r)
    # A bare run dir with no result.json (e.g. an in-progress / crashed run).
    (backend._agent_root / "outcomes" / "runs" / "outcome-incomplete-run").mkdir(
        parents=True
    )
    assert backend.list_runs("testagent") == [r.run_id]


# TEST 8d — export() de-dupes an artifact referenced by both output_files and an
# iteration artifact_path into exactly one ref (load-bearing for artifact-reference portability).


def test_export_dedupes_repeated_artifact_to_one_ref(backend) -> None:
    """The same artifact in output_files + iteration.artifact_path yields one ref."""
    artifact = backend._agent_root / "outcomes" / "runs" / "r1" / "draft.md"
    result = _make_outcome_result(
        run_id="outcome-20260611-190000-dedupe01",
        output_files=[artifact],
        artifact_path=artifact,
    )
    backend.write_result("testagent", result.run_id, result)
    exported = backend.export(None)
    # Rebased relative to agent_root, de-duped to a single ref.
    assert exported.artifact_refs == ["outcomes/runs/r1/draft.md"]


# TEST 8e — a symlinked run dir (or symlinked result.json) escaping agent_root is
# NOT enumerated by list_runs() and NOT read by export() — closes the containment
# escape where export() could read an arbitrary host file (T15 boundary).


def test_list_runs_and_export_refuse_symlinked_run(backend, tmp_path) -> None:
    """A symlinked run dir pointing outside agent_root is never listed or exported."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "result.json").write_text('{"description": "LEAK"}', encoding="utf-8")
    runs_dir = backend._agent_root / "outcomes" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    os.symlink(outside, runs_dir / "outcome-evil")

    assert backend.list_runs("testagent") == []
    exported = backend.export(None)
    assert exported.run_id == ""
    assert b"LEAK" not in exported.result_json_bytes


def test_symlinked_outcomes_ancestor_refused(tmp_path) -> None:
    """A symlinked 'outcomes'/'runs' ANCESTOR escaping the vault is refused.

    safe_resolve_under trusts runs_root as the containment anchor, so a symlinked
    ancestor would otherwise become the trusted root. Containment is anchored at
    agent_root: list_runs yields [], export reads nothing, read_result raises.
    """
    agents_root = tmp_path / "agents"
    agent_root = agents_root / "a"
    agent_root.mkdir(parents=True)
    outside = tmp_path / "evil_outcomes"
    (outside / "runs" / "outcome-x").mkdir(parents=True)
    (outside / "runs" / "outcome-x" / "result.json").write_text(
        '{"description": "ESCAPED"}', encoding="utf-8"
    )
    os.symlink(outside, agent_root / "outcomes")
    backend = FilesystemOutcomeBackend(agents_root, "a")

    assert backend.list_runs("a") == []
    assert b"ESCAPED" not in backend.export(None).result_json_bytes
    with pytest.raises(PathTraversalError):
        backend.read_result("a", "outcome-x")


def test_doctor_fails_on_path_traversal_not_vanished(tmp_path, monkeypatch) -> None:
    """doctor treats read_result() PathTraversalError as FAIL, not benign-vanished.

    PathTraversalError subclasses AtomicAgentsError; the bare-AtomicAgentsError
    branch maps to the benign vanished-run race (PASS). A traversing run must hit
    the dedicated PathTraversalError->FAIL branch instead (defense-in-depth for a
    TOCTOU symlink planted between list_runs() and read_result()).
    """
    import atomic_agents.doctor as doctor_mod

    class _TraversingBackend:
        backend_id = "filesystem"

        def list_runs(self, agent_id):
            return ["outcome-evil"]  # enumerable (e.g. TOCTOU symlink)

        def read_result(self, agent_id, run_id):
            raise PathTraversalError(
                "result.json escaped the vault", child=run_id, root="/vault"
            )

        def capabilities(self):
            from atomic_agents.outcome.types import OutcomeCapabilities

            return OutcomeCapabilities(
                backend_id="filesystem",
                supports_canonical_export=True,
                supports_artifact_storage=True,
            )

    # check_outcome_backend does a local `from atomic_agents.outcome import
    # get_default_outcome_backend`, so patch it at the source module.
    monkeypatch.setattr(
        "atomic_agents.outcome.get_default_outcome_backend",
        lambda agent_root: _TraversingBackend(),
    )
    agent_root = tmp_path / "testagent"
    agent_root.mkdir()
    result = doctor_mod.check_outcome_backend(agent_root)
    assert result.status == doctor_mod.FAIL
    assert "traversal" in result.message.lower()


def test_read_result_rejects_string_output_files(tmp_path: Path) -> None:
    """read_result() raises OutcomeCorrupted when output_files is a string (not list).

    Without the list-shape guard, [Path(p) for p in "/tmp/x"] yields one Path per
    CHARACTER — a silently corrupt OutcomeResult.
    """
    agents_root = tmp_path / "agents"
    backend = FilesystemOutcomeBackend(agents_root, "testagent")
    run_dir = agents_root / "testagent" / "outcomes" / "runs" / "outcome-strfiles"
    run_dir.mkdir(parents=True)
    payload = {
        "run_id": "outcome-strfiles",
        "description": "x",
        "rubric_source": "inline",
        "max_iterations": 1,
        "status": "satisfied",
        "explanation": "",
        "iterations": [],
        "final_iteration_idx": 0,
        "total_cost_usd": 0.0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "started_at": "t",
        "ended_at": "t",
        "output_files": "/tmp/out.md",  # WRONG: a string, not a list
    }
    (run_dir / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(OutcomeCorrupted):
        backend.read_result("testagent", "outcome-strfiles")


def test_main_is_reexported_from_package() -> None:
    """`from atomic_agents.outcome import main` keeps working after the split."""
    from atomic_agents.outcome import main

    assert callable(main)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 9 — list_runs() returns sorted run_ids (spec/42 MUST 3)


def test_list_runs_returns_sorted(backend) -> None:
    """list_runs() MUST return run_ids in lexicographic (sorted) order."""
    r1 = _make_outcome_result(run_id="outcome-20260611-180000-aaa")
    r2 = _make_outcome_result(run_id="outcome-20260611-190000-bbb")
    r3 = _make_outcome_result(run_id="outcome-20260611-200000-ccc")
    backend.write_result("testagent", r1.run_id, r1)
    backend.write_result("testagent", r3.run_id, r3)
    backend.write_result("testagent", r2.run_id, r2)

    run_ids = backend.list_runs("testagent")
    assert run_ids == sorted(run_ids), "list_runs() must return sorted run_ids"
    assert len(run_ids) == 3


# ──────────────────────────────────────────────────────────────────────────────
# TEST 10 — export() returns OutcomeExport type (spec/42 export contract)


def test_export_returns_outcome_export(backend) -> None:
    """export() returns an OutcomeExport instance."""
    result = backend.export()
    assert isinstance(result, OutcomeExport)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 11 — export() returns empty OutcomeExport when no runs (spec/42 export contract)


def test_export_empty_when_no_runs(backend) -> None:
    """export() returns empty OutcomeExport when no runs exist."""
    result = backend.export()
    assert isinstance(result, OutcomeExport)
    assert result.run_id == ""
    assert result.result_json_bytes == b""
    assert result.artifact_refs == []


# ──────────────────────────────────────────────────────────────────────────────
# TEST 12 — export_all() equals export(None) (spec/42 export contract)


def test_export_all_equals_export_none(backend_with_result) -> None:
    """export_all() produces the same result as export(None)."""
    backend, _ = backend_with_result
    result_none = backend.export(None)
    result_all = backend.export_all()
    assert result_none.run_id == result_all.run_id
    assert result_none.result_json_bytes == result_all.result_json_bytes
    assert result_none.artifact_refs == result_all.artifact_refs


# TEST 12b — export_all() is single-run even with MULTIPLE runs present.
# Pins the documented spec/42 §"Export fidelity" divergence from the spec/40
# "all records, no filter" meaning of export_all (follow-up #454). Guards against
# the silent under-export sharp edge: a multi-run vault must NOT drift this into
# an all-runs export without changing the OutcomeExport TYPE.


def test_export_all_is_single_most_recent_run_with_multiple_runs(backend) -> None:
    """export_all() returns ONLY the lexicographically-last run, not all runs."""
    r1 = _make_outcome_result(run_id="outcome-20260611-180000-aaa")
    r2 = _make_outcome_result(run_id="outcome-20260611-190000-bbb")
    r3 = _make_outcome_result(run_id="outcome-20260611-200000-ccc")
    backend.write_result("testagent", r1.run_id, r1)
    backend.write_result("testagent", r3.run_id, r3)
    backend.write_result("testagent", r2.run_id, r2)

    exported = backend.export_all()
    assert exported.run_id == r3.run_id, (
        "export_all() must return only the lexicographically-last run "
        "(documented single-run divergence; see spec/42 §Export fidelity, #454)"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 13 — export() artifact_refs are relative-to-agent_root (spec/42 §Artifact-reference portability)


def test_export_artifact_refs_are_relative(tmp_path: Path) -> None:
    """export() MUST rebase artifact paths to relative-to-agent_root.

    Artifacts under agent_root are exported as relative refs (portable).
    """
    agents_root = tmp_path / "agents"
    backend = FilesystemOutcomeBackend(agents_root, "testagent")
    agent_root = agents_root / "testagent"

    # Create an artifact under agent_root
    run_id = "outcome-20260611-190000-porttest"
    run_dir = agent_root / "outcomes" / "runs" / run_id
    run_dir.mkdir(parents=True)
    artifact = run_dir / "output.md"
    artifact.write_text("# Draft\n\nHello world.", encoding="utf-8")

    result = _make_outcome_result(
        run_id=run_id,
        output_files=[artifact],
        artifact_path=artifact,
    )
    backend.write_result("testagent", run_id, result)

    export = backend.export()
    assert export.run_id == run_id

    # All artifact_refs under agent_root MUST be relative
    for ref in export.artifact_refs:
        assert not Path(ref).is_absolute(), (
            f"artifact_ref {ref!r} must be relative-to-agent_root for portability"
        )
        # Verify it can be reconstructed by joining with agent_root
        reconstructed = agent_root / ref
        assert reconstructed.exists(), (
            f"reconstructed path {reconstructed} from relative ref {ref!r} must exist"
        )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 14 — export() artifact outside agent_root stays absolute (spec/42 §Artifact-reference portability fallback)


def test_export_artifact_outside_agent_root_stays_absolute(tmp_path: Path) -> None:
    """export() MUST preserve absolute paths for artifacts outside agent_root.

    Artifact paths that are NOT under agent_root cannot be made relative — they
    must be exported as absolute paths (is_relative_to fallback per spec/42).
    """
    agents_root = tmp_path / "agents"
    backend = FilesystemOutcomeBackend(agents_root, "testagent")
    agent_root = agents_root / "testagent"

    # Create an artifact OUTSIDE agent_root
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    artifact = external_dir / "external_artifact.md"
    artifact.write_text("External content.", encoding="utf-8")

    run_id = "outcome-20260611-190000-exttest"
    run_dir = agent_root / "outcomes" / "runs" / run_id
    run_dir.mkdir(parents=True)

    result = _make_outcome_result(
        run_id=run_id,
        output_files=[artifact],
        artifact_path=artifact,
    )
    backend.write_result("testagent", run_id, result)

    export = backend.export()
    assert export.run_id == run_id
    # External artifacts must be preserved as absolute paths
    assert len(export.artifact_refs) >= 1
    external_refs = [r for r in export.artifact_refs if Path(r).is_absolute()]
    assert len(external_refs) >= 1, (
        "Artifacts outside agent_root MUST be exported as absolute paths"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 15 — export() result_json_bytes is byte-identical to written file


def test_export_result_json_bytes_identical_to_file(tmp_path: Path) -> None:
    """export() result_json_bytes MUST be byte-identical to the on-disk result.json."""
    agents_root = tmp_path / "agents"
    backend = FilesystemOutcomeBackend(agents_root, "testagent")
    agent_root = agents_root / "testagent"

    result = _make_outcome_result()
    backend.write_result("testagent", result.run_id, result)

    # Read the file directly
    result_path = agent_root / "outcomes" / "runs" / result.run_id / "result.json"
    on_disk_bytes = result_path.read_bytes()

    export = backend.export()
    assert export.result_json_bytes == on_disk_bytes, (
        "export().result_json_bytes MUST be byte-identical to the on-disk result.json"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 16 — OutcomeResult/IterationRecord are mutable (spec/42 mutable-dataclass note)


def test_outcome_result_is_mutable() -> None:
    """OutcomeResult MUST be a mutable dataclass (not frozen=True).

    The outcome layer is a state machine during the run — status/iterations/
    output_files are mutated in-place across the OutcomeRunner loop.
    """
    result = _make_outcome_result()
    result.status = "max_iterations_reached"
    assert result.status == "max_iterations_reached"


def test_iteration_record_is_mutable() -> None:
    """IterationRecord MUST be a mutable dataclass (not frozen=True)."""
    rec = _make_iteration_record()
    rec.agent_cost_usd = 9.99
    assert rec.agent_cost_usd == 9.99


# ──────────────────────────────────────────────────────────────────────────────
# TEST 17 — OutcomeCapabilities is frozen (spec/42 MUST 2)


def test_outcome_capabilities_is_frozen() -> None:
    """OutcomeCapabilities MUST be a frozen dataclass (immutable value object)."""
    caps = OutcomeCapabilities(backend_id="test")
    with pytest.raises((TypeError, AttributeError)):
        caps.supports_canonical_export = True  # type: ignore[misc]


# ──────────────────────────────────────────────────────────────────────────────
# TEST 18 — OutcomeCapabilities(backend_id='x') valid with all defaults False (spec/42 MUST 2)


def test_outcome_capabilities_minimal_construction() -> None:
    """OutcomeCapabilities(backend_id='x') must be valid with all defaults False."""
    caps = OutcomeCapabilities(backend_id="x")
    assert caps.backend_id == "x"
    assert caps.supports_canonical_export is False
    assert caps.supports_artifact_storage is False


# ──────────────────────────────────────────────────────────────────────────────
# TEST 19 — backend_id stability (spec/42 MUST 2)


def test_filesystem_backend_id_is_filesystem(backend) -> None:
    """FilesystemOutcomeBackend.backend_id MUST be 'filesystem'."""
    assert backend.backend_id == "filesystem"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 20 — OutcomeCapabilities field types are bool (spec/42 MUST 2)


def test_capabilities_fields_are_bool(backend) -> None:
    """All capability flags MUST be Python bool (not truthy int or None)."""
    caps = backend.capabilities()
    assert isinstance(caps.supports_canonical_export, bool)
    assert isinstance(caps.supports_artifact_storage, bool)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 21 — supports_canonical_export=True for filesystem (spec/42 MUST 2 / spec/40)


def test_filesystem_advertises_canonical_export(backend) -> None:
    """FilesystemOutcomeBackend MUST advertise supports_canonical_export=True."""
    caps = backend.capabilities()
    assert caps.supports_canonical_export is True, (
        "FilesystemOutcomeBackend must advertise supports_canonical_export=True "
        "per spec/42 §'spec/40 addendum'"
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 22 — supports_artifact_storage=True for filesystem (spec/42 MUST 2)


def test_filesystem_advertises_artifact_storage(backend) -> None:
    """FilesystemOutcomeBackend MUST advertise supports_artifact_storage=True."""
    caps = backend.capabilities()
    assert caps.supports_artifact_storage is True


# ──────────────────────────────────────────────────────────────────────────────
# TEST 23 — env var dispatches registered custom backend


def test_env_var_dispatches_custom_backend(tmp_path: Path, monkeypatch) -> None:
    """ATOMIC_AGENTS_OUTCOME_BACKEND=custom dispatches to the registered class."""
    from atomic_agents.outcome import (
        register_outcome_backend,
        unregister_outcome_backend,
        get_default_outcome_backend,
    )

    # Build a minimal stand-in that satisfies the constructor signature
    class _CustomOutcomeBackend:
        def __init__(self, agents_root, agent_name, **kwargs):
            self._agent_root = agents_root / agent_name
            self.backend_id = "custom"

    register_outcome_backend("custom", _CustomOutcomeBackend)
    try:
        monkeypatch.setenv("ATOMIC_AGENTS_OUTCOME_BACKEND", "custom")
        agent_root = tmp_path / "agent"
        agent_root.mkdir()
        backend = get_default_outcome_backend(agent_root)
        assert backend.backend_id == "custom"
    finally:
        unregister_outcome_backend("custom")


# ──────────────────────────────────────────────────────────────────────────────
# TEST 24 — get_outcome_backend() raises BackendNotRegistered for unknown id


def test_get_outcome_backend_unknown_raises(tmp_path: Path) -> None:
    """get_outcome_backend() MUST raise BackendNotRegistered for an unknown id."""
    from atomic_agents.outcome import get_outcome_backend
    from atomic_agents.exceptions import BackendNotRegistered

    with pytest.raises(BackendNotRegistered):
        get_outcome_backend("nonexistent_backend_xyz")


# ──────────────────────────────────────────────────────────────────────────────
# TEST 25–28 — _redact_for_error_message


def test_redact_url_value() -> None:
    """_redact_for_error_message redacts URL: returns scheme://..."""
    from atomic_agents.outcome import _redact_for_error_message

    result = _redact_for_error_message("postgres://user:pass@host/db")
    assert result == "postgres://..."


def test_redact_dsn_value() -> None:
    """_redact_for_error_message redacts schemeless DSN."""
    from atomic_agents.outcome import _redact_for_error_message

    result = _redact_for_error_message("user:pass@host/db")
    assert result == "[redacted-connection-string]"


def test_redact_truncates_long_value() -> None:
    """_redact_for_error_message truncates values > 32 chars."""
    from atomic_agents.outcome import _redact_for_error_message

    long_val = "a" * 40
    result = _redact_for_error_message(long_val)
    assert result == "a" * 32 + "..."


def test_redact_passthrough_short_value() -> None:
    """_redact_for_error_message passes through short safe values unchanged."""
    from atomic_agents.outcome import _redact_for_error_message

    result = _redact_for_error_message("filesystem")
    assert result == "filesystem"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 29 — get_default_outcome_backend() empty-string env var uses filesystem


def test_empty_env_var_uses_filesystem_default(tmp_path: Path, monkeypatch) -> None:
    """ATOMIC_AGENTS_OUTCOME_BACKEND='' falls through to filesystem default."""
    from atomic_agents.outcome import get_default_outcome_backend

    monkeypatch.setenv("ATOMIC_AGENTS_OUTCOME_BACKEND", "")
    agent_root = tmp_path / "testagent"
    agent_root.mkdir()
    backend = get_default_outcome_backend(agent_root)
    assert backend.backend_id == "filesystem"


# ──────────────────────────────────────────────────────────────────────────────
# TEST 30 — Golden-file: write_result() byte-identical to old runner output


def test_golden_file_byte_identity(tmp_path: Path) -> None:
    """write_result() MUST produce byte-identical JSON to the existing runner.

    This test verifies the artifact-reference-portability ruling: on-disk result.json STAYS
    byte-identical to what OutcomeRunner._write_result_json() produces.

    The reference bytes are produced by the REAL production serializer
    (OutcomeRunner._write_result_json in _outcome_impl.py), NOT a test-local
    re-implementation. If a future edit changes _write_result_json, this test
    FAILS — that is the zero-behavior-change guarantee actually pinned to the
    production code path (Tier B ruling: "TEST-ENFORCED, not asserted").
    """
    from atomic_agents.outcome import OutcomeRunner

    agents_root = tmp_path / "agents"
    backend = FilesystemOutcomeBackend(agents_root, "testagent")
    agent_root = agents_root / "testagent"

    # Create a result with Path fields to exercise full serialization
    run_dir = agent_root / "outcomes" / "runs" / "outcome-20260611-golden-test"
    run_dir.mkdir(parents=True)
    artifact = run_dir / "output.md"
    artifact.write_text("# Draft\n\nGolden test.", encoding="utf-8")

    result = _make_outcome_result(
        run_id="outcome-20260611-golden-test",
        output_files=[artifact],
        artifact_path=artifact,
    )

    # Reference bytes: invoke the REAL runner serializer into a separate dir.
    # OutcomeRunner.__init__ is side-effect-free w.r.t. result.json; we never
    # call run() (no LLM). _write_result_json(output_dir, result) writes
    # result.json into output_dir exactly as production does.
    runner = OutcomeRunner(agents_root=agents_root, agent_name="testagent")
    reference_dir = tmp_path / "reference_run"
    reference_dir.mkdir(parents=True)
    runner._write_result_json(reference_dir, result)
    expected_bytes = (reference_dir / "result.json").read_bytes()

    # Write via FilesystemOutcomeBackend
    backend.write_result("testagent", result.run_id, result)
    result_path = run_dir / "result.json"
    actual_bytes = result_path.read_bytes()

    assert actual_bytes == expected_bytes, (
        "write_result() MUST produce byte-identical JSON to the REAL runner's "
        "_write_result_json() (artifact-reference-portability ruling). "
        "If this fails, write_result() and OutcomeRunner._write_result_json have "
        "diverged — the zero-behavior-change guarantee is broken."
    )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 31 — from_dict() / to_dict() round-trip with Path fields


def test_outcome_result_to_dict_from_dict_roundtrip() -> None:
    """OutcomeResult to_dict() → from_dict() must preserve all fields including Paths."""
    artifact = Path("/tmp/test_artifact.md")
    result = _make_outcome_result(
        output_files=[artifact],
        artifact_path=artifact,
    )
    d = result.to_dict()
    # to_dict() must produce JSON-serializable values (strings, not Paths)
    json_str = json.dumps(d)  # Must not raise
    assert isinstance(json_str, str)

    # Verify Path coercion happened
    assert all(isinstance(p, str) for p in d["output_files"])
    for rec in d["iterations"]:
        if rec.get("artifact_path") is not None:
            assert isinstance(rec["artifact_path"], str)

    # Round-trip back
    restored = OutcomeResult.from_dict(json.loads(json_str))
    assert restored.run_id == result.run_id
    # Path fields MUST be coerced back to Path objects
    assert all(isinstance(p, Path) for p in restored.output_files)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 32 — OutcomeResult.from_dict() coerces output_files to list[Path]


def test_from_dict_coerces_output_files_to_path() -> None:
    """OutcomeResult.from_dict() MUST coerce output_files strings to list[Path]."""
    result = _make_outcome_result()
    d = result.to_dict()
    d["output_files"] = ["/tmp/file1.md", "/tmp/file2.md"]

    restored = OutcomeResult.from_dict(d)
    assert all(isinstance(p, Path) for p in restored.output_files)
    assert restored.output_files[0] == Path("/tmp/file1.md")


# ──────────────────────────────────────────────────────────────────────────────
# TEST 33 — IterationRecord.from_dict() coerces artifact_path to Path


def test_iteration_record_from_dict_coerces_artifact_path() -> None:
    """IterationRecord.from_dict() MUST coerce artifact_path string to Path."""
    rec = _make_iteration_record(artifact_path=Path("/tmp/artifact.md"))
    d = rec.to_dict()
    assert isinstance(d["artifact_path"], str)

    restored = IterationRecord.from_dict(d)
    assert isinstance(restored.artifact_path, Path)
    assert restored.artifact_path == Path("/tmp/artifact.md")


# ──────────────────────────────────────────────────────────────────────────────
# TEST 34 — doctor.check_outcome_backend returns PASS for empty agent


def test_doctor_check_outcome_backend_pass_empty_agent(tmp_path: Path) -> None:
    """check_outcome_backend MUST return PASS for an agent with no outcome runs.

    Agents that have never completed an outcome run have no outcomes/ dir.
    This is NOT a failure condition (matching check_goal_backend for no goal.md).
    """
    from atomic_agents.doctor import check_outcome_backend, PASS

    agent_root = tmp_path / "testagent"
    agent_root.mkdir()
    result = check_outcome_backend(agent_root)
    assert result.status == PASS
    assert result.detail.get("outcome_runs_present") is False
    assert result.detail.get("run_count") == 0


# ──────────────────────────────────────────────────────────────────────────────
# TEST 35 — doctor.check_outcome_backend returns PASS with runs + dual-probe


def test_doctor_check_outcome_backend_pass_with_runs(tmp_path: Path) -> None:
    """check_outcome_backend MUST return PASS when runs exist and read_result works.

    The dual-probe pattern fires: list_runs() finds runs, read_result() is
    exercised on the most recent run. Both must succeed for PASS.
    """
    from atomic_agents.doctor import check_outcome_backend, PASS

    agents_root = tmp_path / "agents"
    backend = FilesystemOutcomeBackend(agents_root, "testagent")
    result = _make_outcome_result()
    backend.write_result("testagent", result.run_id, result)

    agent_root = agents_root / "testagent"
    check_result = check_outcome_backend(agent_root)
    assert check_result.status == PASS
    assert check_result.detail.get("outcome_runs_present") is True
    assert check_result.detail.get("read_result_probed") is True
    assert check_result.detail.get("run_count") == 1


# ──────────────────────────────────────────────────────────────────────────────
# TEST 36 — doctor.check_outcome_backend returns FAIL for bad env var


def test_doctor_check_outcome_backend_fail_bad_env(tmp_path: Path, monkeypatch) -> None:
    """check_outcome_backend MUST return FAIL when ATOMIC_AGENTS_OUTCOME_BACKEND
    names an unregistered backend.
    """
    from atomic_agents.doctor import check_outcome_backend, FAIL

    monkeypatch.setenv("ATOMIC_AGENTS_OUTCOME_BACKEND", "nonexistent_outcome_backend")
    agent_root = tmp_path / "testagent"
    agent_root.mkdir()
    result = check_outcome_backend(agent_root)
    assert result.status == FAIL


# ──────────────────────────────────────────────────────────────────────────────
# TEST 37 — export() with corrupt-but-present result.json: bytes preserved,
# artifact_refs empty (the inner json.JSONDecodeError → data={} branch)


def test_export_corrupt_json_preserves_bytes_empty_refs(tmp_path: Path) -> None:
    """export() with an unparseable result.json keeps the raw bytes but yields
    no artifact_refs (the inner json.JSONDecodeError → data={} fallback).

    The on-disk bytes are still emitted verbatim (an operator can inspect the
    corruption downstream); only artifact rebasing is skipped because there is
    no parseable structure to read output_files/iterations from.
    """
    agents_root = tmp_path / "agents"
    backend = FilesystemOutcomeBackend(agents_root, "testagent")
    run_dir = agents_root / "testagent" / "outcomes" / "runs" / "outcome-corrupt-export"
    run_dir.mkdir(parents=True)
    (run_dir / "result.json").write_text("NOT VALID JSON {{{", encoding="utf-8")

    export = backend.export()
    assert export.run_id == "outcome-corrupt-export"
    assert export.result_json_bytes == b"NOT VALID JSON {{{"
    assert export.artifact_refs == []


# TEST 37b — export() with valid-JSON-WRONG-SHAPE result.json degrades to empty
# refs (bytes preserved) rather than crashing the whole agent's export. Without
# the isinstance(dict) guard, data.get() on a list/scalar leaks AttributeError.


@pytest.mark.parametrize("wrong_shape", ["[1, 2, 3]", '"hello"', "42", "null"])
def test_export_wrong_shape_json_degrades_to_empty_refs(
    tmp_path: Path, wrong_shape
) -> None:
    """export() with non-object JSON keeps bytes, yields no refs, does not raise."""
    agents_root = tmp_path / "agents"
    backend = FilesystemOutcomeBackend(agents_root, "testagent")
    run_dir = (
        agents_root / "testagent" / "outcomes" / "runs" / "outcome-wrongshape-export"
    )
    run_dir.mkdir(parents=True)
    (run_dir / "result.json").write_text(wrong_shape, encoding="utf-8")

    export = backend.export()
    assert export.run_id == "outcome-wrongshape-export"
    assert export.result_json_bytes == wrong_shape.encode("utf-8")
    assert export.artifact_refs == []


# TEST 37c — export() with a dict result.json whose NESTED fields are malformed
# (output_files / iterations not lists, iteration not a dict, non-string paths)
# degrades to empty refs rather than crashing. The top-level isinstance guard
# does not protect nested shapes; each nested access is coerced defensively.


@pytest.mark.parametrize(
    "malformed",
    [
        '{"output_files": "abc"}',
        '{"output_files": 5}',
        '{"output_files": {"k": "v"}}',
        '{"iterations": "xyz"}',
        '{"iterations": [1, 2, 3]}',
        '{"iterations": 7}',
        '{"output_files": [123, null]}',
    ],
)
def test_export_malformed_nested_fields_degrade_to_empty_refs(
    tmp_path: Path, malformed
) -> None:
    """export() never crashes on malformed nested output_files/iterations shapes."""
    agents_root = tmp_path / "agents"
    backend = FilesystemOutcomeBackend(agents_root, "testagent")
    run_dir = (
        agents_root / "testagent" / "outcomes" / "runs" / "outcome-malformed-nested"
    )
    run_dir.mkdir(parents=True)
    (run_dir / "result.json").write_text(malformed, encoding="utf-8")

    export = backend.export()
    assert export.run_id == "outcome-malformed-nested"
    assert export.artifact_refs == []


# ──────────────────────────────────────────────────────────────────────────────
# TEST 38 — export() resolve-both-sides: symlinked root + resolved artifact path
# still rebases to a relative ref (the comparison-only .resolve() guard)


def test_export_resolve_both_sides_symlink_rebases_relative(tmp_path: Path) -> None:
    """export() MUST rebase to a relative ref even when the backend root is
    UNRESOLVED (constructed via a symlink) while result.json holds RESOLVED
    absolute artifact paths.

    This pins the comparison-only .resolve() on BOTH sides (filesystem.py
    ~258-274): without it, a symlinked /tmp (macOS /tmp → /private/tmp) or an
    unresolved agents_root would silently fall back to a non-portable absolute
    ref. This is the load-bearing artifact-reference-portability guard.
    """
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    # Backend constructed through the SYMLINK path (stays unresolved internally).
    backend = FilesystemOutcomeBackend(link, "testagent")
    run_id = "outcome-20260611-symlink"
    run_dir = link / "testagent" / "outcomes" / "runs" / run_id
    run_dir.mkdir(parents=True)
    artifact = run_dir / "output.md"
    artifact.write_text("# Draft\n", encoding="utf-8")

    # Store the RESOLVED absolute path (mimics the runner persisting real paths).
    resolved_artifact = artifact.resolve()
    result = _make_outcome_result(
        run_id=run_id,
        output_files=[resolved_artifact],
        artifact_path=resolved_artifact,
    )
    backend.write_result("testagent", run_id, result)

    export = backend.export()
    assert export.artifact_refs, "expected at least one artifact_ref"
    for ref in export.artifact_refs:
        assert not Path(ref).is_absolute(), (
            f"artifact_ref {ref!r} must rebase to relative via resolve-both-sides "
            "even when the root was constructed through a symlink"
        )


# ──────────────────────────────────────────────────────────────────────────────
# TEST 39 — get_default_outcome_backend() unknown env var raises BackendNotRegistered
# (direct factory path, complementing the doctor-mediated TEST 36)


def test_get_default_unknown_env_raises(tmp_path: Path, monkeypatch) -> None:
    """get_default_outcome_backend() MUST raise BackendNotRegistered when
    ATOMIC_AGENTS_OUTCOME_BACKEND names an unregistered backend.

    Exercises the factory's fail-fast raise directly (the __init__.py:252-260
    branch), not just via doctor's wrapped FAIL.
    """
    from atomic_agents.outcome import get_default_outcome_backend
    from atomic_agents.exceptions import BackendNotRegistered

    monkeypatch.setenv("ATOMIC_AGENTS_OUTCOME_BACKEND", "totally-bogus-backend")
    agent_root = tmp_path / "testagent"
    agent_root.mkdir()
    with pytest.raises(BackendNotRegistered):
        get_default_outcome_backend(agent_root)


# ──────────────────────────────────────────────────────────────────────────────
# TEST 40 — doctor.check_outcome_backend: list_runs() raises → FAIL (light probe)


def test_doctor_check_outcome_backend_list_runs_raises_fail(
    tmp_path: Path, monkeypatch
) -> None:
    """check_outcome_backend MUST FAIL when the light probe list_runs() raises."""
    import atomic_agents.outcome as omod
    from atomic_agents.doctor import check_outcome_backend, FAIL

    class _BoomList(FilesystemOutcomeBackend):
        def list_runs(self, agent_id):  # type: ignore[override]
            raise RuntimeError("boom-list")

    monkeypatch.setattr(
        omod,
        "get_default_outcome_backend",
        lambda root: _BoomList(root.parent, root.name),
    )
    agent_root = tmp_path / "testagent"
    agent_root.mkdir()
    result = check_outcome_backend(agent_root)
    assert result.status == FAIL
    assert "list_runs" in result.message


# ──────────────────────────────────────────────────────────────────────────────
# TEST 41 — doctor.check_outcome_backend: read_result() OutcomeCorrupted → FAIL
# (heavy probe on a present-but-corrupt result.json)


def test_doctor_check_outcome_backend_corrupt_read_fail(tmp_path: Path) -> None:
    """check_outcome_backend MUST FAIL when a present run's result.json is corrupt.

    The heavy probe (read_result on the most-recent run) hits OutcomeCorrupted,
    which is a real FAIL — not the benign vanished-run race.
    """
    from atomic_agents.doctor import check_outcome_backend, FAIL

    agents_root = tmp_path / "agents"
    FilesystemOutcomeBackend(agents_root, "testagent")
    run_dir = agents_root / "testagent" / "outcomes" / "runs" / "outcome-corrupt"
    run_dir.mkdir(parents=True)
    (run_dir / "result.json").write_text("{ bad json", encoding="utf-8")

    result = check_outcome_backend(agents_root / "testagent")
    assert result.status == FAIL


# ──────────────────────────────────────────────────────────────────────────────
# TEST 42 — doctor.check_outcome_backend: vanished-run TOCTOU is benign → PASS
# (list_runs returns a run, read_result raises bare AtomicAgentsError)


def test_doctor_check_outcome_backend_vanished_run_benign_pass(
    tmp_path: Path, monkeypatch
) -> None:
    """check_outcome_backend MUST PASS when a listed run vanishes before read.

    list_runs() returns a run_id but read_result() raises a bare
    AtomicAgentsError (absent file = concurrent cleanup). That is a benign
    TOCTOU race, not corruption — PASS with read_result_vanished=True,
    read_result_probed=False, run_count keyed to len(run_ids).
    """
    import atomic_agents.outcome as omod
    from atomic_agents.doctor import check_outcome_backend, PASS
    from atomic_agents.exceptions import AtomicAgentsError

    class _VanishingBackend(FilesystemOutcomeBackend):
        def list_runs(self, agent_id):  # type: ignore[override]
            return ["outcome-ghost-run"]

        def read_result(self, agent_id, run_id):  # type: ignore[override]
            raise AtomicAgentsError("result.json vanished between list and read")

    monkeypatch.setattr(
        omod,
        "get_default_outcome_backend",
        lambda root: _VanishingBackend(root.parent, root.name),
    )
    agent_root = tmp_path / "agents" / "testagent"
    agent_root.mkdir(parents=True)
    result = check_outcome_backend(agent_root)
    assert result.status == PASS
    assert result.detail.get("read_result_vanished") is True
    assert result.detail.get("read_result_probed") is False
    assert result.detail.get("run_count") == 1


# ──────────────────────────────────────────────────────────────────────────────
# TEST 44 — doctor.check_outcome_backend: read_result() raises an UNEXPECTED
# (non-AtomicAgentsError, non-OutcomeCorrupted) error → FAIL (defensive catch-all)


def test_doctor_check_outcome_backend_unexpected_read_error_fail(
    tmp_path: Path, monkeypatch
) -> None:
    """check_outcome_backend MUST FAIL when read_result() raises an unexpected
    error type (not OutcomeCorrupted, not bare AtomicAgentsError).

    Pins the defensive ``except Exception`` catch-all in the heavy probe: a
    backend bug that surfaces as e.g. RuntimeError is a real FAIL, not a benign
    vanished-run race.
    """
    import atomic_agents.outcome as omod
    from atomic_agents.doctor import check_outcome_backend, FAIL

    class _BoomRead(FilesystemOutcomeBackend):
        def list_runs(self, agent_id):  # type: ignore[override]
            return ["outcome-boom-read"]

        def read_result(self, agent_id, run_id):  # type: ignore[override]
            raise RuntimeError("unexpected backend explosion")

    monkeypatch.setattr(
        omod,
        "get_default_outcome_backend",
        lambda root: _BoomRead(root.parent, root.name),
    )
    agent_root = tmp_path / "agents" / "testagent"
    agent_root.mkdir(parents=True)
    result = check_outcome_backend(agent_root)
    assert result.status == FAIL
    assert "unexpected" in result.message.lower()


# ──────────────────────────────────────────────────────────────────────────────
# TEST 43 — AtomicAgent.outcome_backend per-agent handle wiring (agent.py)


def test_agent_outcome_backend_attribute_is_wired(tmp_path: Path, monkeypatch) -> None:
    """AtomicAgent.__init__ MUST set self.outcome_backend to a backend instance.

    Per-agent handle wiring: `AtomicAgent.outcome_backend` is the per-agent
    inspection/coordinator HANDLE (set at construction, resolves to the
    filesystem default + doctor access). It is NOT the live write path — that
    is `OutcomeRunner.outcome_backend`, exercised by TEST 30 and the goldens in
    `test_outcome_adoption_golden.py` (#448 PR2). This test pins that the public
    attribute exists and resolves to the filesystem default so the PR3
    coordinator handoff has a stable handle to build on.
    """
    from atomic_agents.agent import AtomicAgent

    agents_root = tmp_path / "agents"
    agent_dir = agents_root / "scaffold-agent"
    persona_dir = agent_dir / "persona"
    persona_dir.mkdir(parents=True)
    (persona_dir / "IDENTITY.md").write_text("# Identity\nScaffold test agent.")
    (agent_dir / "tools.md").write_text("## Read paths\n- ~/docs/\n")
    (agent_dir / "model.md").write_text("## Default model\nclaude-haiku-4-5-20251001\n")
    (agent_dir / "memory").mkdir()
    (agent_dir / "log").mkdir()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")

    agent = AtomicAgent(name="scaffold-agent", agents_root=agents_root)
    assert hasattr(agent, "outcome_backend")
    assert agent.outcome_backend.backend_id == "filesystem"

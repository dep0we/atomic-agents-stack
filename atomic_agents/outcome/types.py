"""Canonical types for the OutcomeBackend Protocol (spec/42).

NOTE: OutcomeResult and IterationRecord are MUTABLE dataclasses — deliberate
divergence from the frozen-DTO convention in logs/types.py. The outcome layer
is a state machine DURING the run: status/explanation/iterations/output_files
are mutated across the OutcomeRunner loop. Freezing would break the mutation
pattern. See goal/types.py for the GoalBackend analog.

OutcomeCapabilities is frozen=True — frozen dataclass convention matches every
other *Capabilities type (LockCapabilities, LogCapabilities, GoalCapabilities,
etc.) for backward-compatible additive extension.

OutcomeExport is a dataclass subclassing ExportableResult (spec/40).

Field ordering for OutcomeResult / IterationRecord: MUST match the declaration
order in the original outcome.py dataclasses. asdict() in write_result() and
to_dict() both depend on field declaration order for byte-identical JSON output
(asdict() is order-sensitive).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path

from .._export_base import ExportableResult


# ──────────────────────────────────────────────────────────────────
# Mutable state-machine types (diverge from frozen-DTO convention)


@dataclass
class IterationRecord:
    """Record of one agent+judge iteration within an OutcomeResult.

    MUTABLE — the outcome loop mutates per-iteration cost/token fields
    inline. Deliberate divergence from logs/types.py frozen-DTO convention.
    """

    iteration: int  # 0-indexed
    agent_response: str
    agent_input_tokens: int
    agent_output_tokens: int
    agent_cost_usd: float
    agent_latency_ms: int
    judge_response_raw: str
    judge_verdict: dict  # parsed: {satisfied, criterion_results, explanation, ...}
    judge_cost_usd: float
    judge_input_tokens: int
    judge_output_tokens: int
    artifact_path: Path | None  # if the agent wrote a file
    timestamp: str  # ISO 8601

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict with Path fields coerced to str.

        Uses asdict() as the base to preserve field declaration order
        (matching _write_result_json at _outcome_impl.py:721-731). Path fields
        are coerced to str explicitly because asdict() does NOT convert Path
        objects to str — json.dumps() cannot serialize Path objects.
        """
        data = asdict(self)
        if data.get("artifact_path") is not None:
            data["artifact_path"] = str(self.artifact_path)
        return data

    @classmethod
    def from_dict(cls, d: dict) -> "IterationRecord":
        """Reconstruct from a result.json on-disk dict (absolute paths).

        NOTE: This method reconstructs from on-disk result.json format where
        artifact_path is an absolute path string. Do NOT use this to
        reconstruct from an OutcomeExport's artifact_refs (which are
        agent_root-relative strings) — those carry relative paths that
        would not resolve without re-rooting.
        """
        return cls(
            iteration=d["iteration"],
            agent_response=d["agent_response"],
            agent_input_tokens=d["agent_input_tokens"],
            agent_output_tokens=d["agent_output_tokens"],
            agent_cost_usd=d["agent_cost_usd"],
            agent_latency_ms=d["agent_latency_ms"],
            judge_response_raw=d["judge_response_raw"],
            judge_verdict=d.get("judge_verdict", {}),
            judge_cost_usd=d["judge_cost_usd"],
            judge_input_tokens=d["judge_input_tokens"],
            judge_output_tokens=d["judge_output_tokens"],
            artifact_path=Path(d["artifact_path"]) if d.get("artifact_path") else None,
            timestamp=d["timestamp"],
        )


@dataclass
class OutcomeResult:
    """Complete result of an OutcomeRunner.run() call.

    MUTABLE — the runner mutates status/explanation/iterations/output_files
    during the iterate-to-rubric loop. Deliberate divergence from
    frozen-DTO convention. See module docstring.

    output_files and artifact_path stay Path-typed on the dataclass.
    Portability (relative-path refs) is handled at export(), not by
    retyping the dataclass (per artifact-reference-portability ruling,
    spec/42 §"Option C").
    """

    run_id: str
    description: str
    rubric_source: str  # "<agent>/outcomes/foo.md" or "inline"
    max_iterations: int
    status: str  # 'satisfied' | 'max_iterations_reached' | 'failed' | 'interrupted'
    explanation: str  # final judge explanation
    iterations: list[IterationRecord] = field(default_factory=list)
    final_iteration_idx: int = -1
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    started_at: str = ""  # ISO 8601
    ended_at: str = ""
    output_files: list[Path] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict with Path fields coerced to str.

        Uses asdict() as the base to preserve field declaration order
        (matching _write_result_json's asdict() call at _outcome_impl.py:725).
        Path fields in output_files and nested IterationRecord.artifact_path
        are coerced to str because json.dumps() cannot serialize Path objects.

        This is the on-disk serialization format. The export() path uses
        export-specific path rebasing on top of this dict.
        """
        data = asdict(self)
        # Convert output_files Path list to str list
        data["output_files"] = [str(p) for p in self.output_files]
        # Convert per-iteration artifact_path from Path to str
        for rec in data["iterations"]:
            if rec.get("artifact_path") is not None:
                rec["artifact_path"] = str(rec["artifact_path"])
        return data

    @classmethod
    def from_dict(cls, d: dict) -> "OutcomeResult":
        """Reconstruct from a result.json on-disk dict (absolute paths).

        NOTE: This method reconstructs from on-disk result.json format where
        output_files and artifact_path are absolute path strings. Do NOT use
        this to reconstruct from an OutcomeExport's artifact_refs (which are
        agent_root-relative strings that would not resolve without re-rooting).

        Path fields are coerced back to Path objects so callers can use
        result.output_files[0].read_text() etc.
        """
        # Validate nested container shapes before coercing: a corrupt result.json
        # may set iterations/output_files to a non-list (e.g. "output_files":
        # "/tmp/x" would otherwise become one Path per CHARACTER). read_result()
        # wraps the TypeError into OutcomeCorrupted.
        raw_iterations = d.get("iterations", [])
        if not isinstance(raw_iterations, list):
            raise TypeError(
                f"iterations must be a list, got {type(raw_iterations).__name__}"
            )
        raw_output_files = d.get("output_files", [])
        if not isinstance(raw_output_files, list):
            raise TypeError(
                f"output_files must be a list, got {type(raw_output_files).__name__}"
            )
        iterations = [IterationRecord.from_dict(r) for r in raw_iterations]
        return cls(
            run_id=d["run_id"],
            description=d["description"],
            rubric_source=d["rubric_source"],
            max_iterations=d["max_iterations"],
            status=d["status"],
            explanation=d["explanation"],
            iterations=iterations,
            final_iteration_idx=d.get("final_iteration_idx", -1),
            total_cost_usd=d.get("total_cost_usd", 0.0),
            total_input_tokens=d.get("total_input_tokens", 0),
            total_output_tokens=d.get("total_output_tokens", 0),
            started_at=d.get("started_at", ""),
            ended_at=d.get("ended_at", ""),
            output_files=[Path(p) for p in raw_output_files],
        )


# ──────────────────────────────────────────────────────────────────
# Frozen value objects


@dataclass(frozen=True)
class OutcomeCapabilities:
    """Per-backend capability declaration for OutcomeBackend (spec/42).

    Matches the frozen-dataclass convention of every other *Capabilities type.
    All capability booleans have defaults=False so new fields can be added at
    the end without breaking existing instantiation sites.

    Fields:
        backend_id: stable backend identifier string (required, no default).
        supports_canonical_export: True when the backend implements the
            spec/40 Exportable Protocol. FilesystemOutcomeBackend=True.
            Default False so existing instantiation sites without this kwarg
            keep working (backward-compatibility pattern from LogCapabilities).
        supports_artifact_storage: True when the backend stores the agent's
            output file artifacts in run directories under outcomes/runs/.
            FilesystemOutcomeBackend=True (run dir holds artifact files per
            spec/14 §"File layout").

    DEFERRED: supports_run_query — deferred until a list/enumerate-by-filter
        method ships on the Protocol (Principle #12 verify-before-claim:
        a capability bool is a conformance-enforced claim today, not a
        roadmap marker; adding default-False later is NON-breaking).

    Field ordering: backend_id (required, no default) first so positional
    construction OutcomeCapabilities("filesystem") is meaningful; capability
    booleans with defaults last so adding a new field at the end does not
    break existing instantiation sites.
    """

    backend_id: str
    supports_canonical_export: bool = False
    supports_artifact_storage: bool = False


# ──────────────────────────────────────────────────────────────────
# Export result type


@dataclass
class OutcomeExport(ExportableResult):
    """Canonical export from an OutcomeBackend (spec/40 §"Per-backend export contracts").

    Carries the result.json bytes for one completed outcome run, plus artifact
    references (relative-to-agent_root paths, NOT embedded bytes per Tier B
    export ruling — spec/42 §"Option C").

    Fields:
        run_id: the run identifier (directory name under outcomes/runs/).
        result_json_bytes: raw bytes of result.json for this run.
        artifact_refs: list of artifact path strings, relative to agent_root
            (e.g. "outcomes/runs/outcome-2026.../artifact.md").
            Absolute when the artifact lives outside agent_root (is_relative_to
            fallback per spec/42 §"Option C").
        backend_id: stable backend identifier.
        scope: agent root path as a string.

    IMPORTANT: artifact_refs are agent_root-relative strings, NOT reconstructable
    as OutcomeResult.artifact_path without re-rooting. Do NOT pass artifact_refs
    back through OutcomeResult.from_dict() — that method is for on-disk result.json
    (absolute paths) only.
    """

    run_id: str
    result_json_bytes: bytes
    artifact_refs: list[str]  # relative-to-agent_root or absolute (fallback)
    backend_id: str
    scope: str  # agent root path as a string

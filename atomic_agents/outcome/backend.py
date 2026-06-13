"""OutcomeBackend Protocol — the contract every outcome implementation satisfies.

This is one of the open protocols in the protocol-pattern series (spec/42).
It decouples the outcome storage layer from the OutcomeRunner business logic,
so alternate outcome storage backends (Postgres, cloud storage) can register
without forking the framework.

Protocol method surface — THIN envelope-only (per arc ruling):

  write_result(agent_id, run_id, result)  — write result.json for one run
  read_result(agent_id, run_id)           — read + reconstruct OutcomeResult
  list_runs(agent_id)                     — enumerate run_ids
  export(query=None)                      — spec/40 canonical export
  export_all()                            — convenience wrapper
  capabilities()                          — return OutcomeCapabilities

Pure computation stays ABOVE the Protocol in OutcomeRunner:
  artifact-file discovery (output_dir.glob diffing between iterations),
  output_dir resolution, run_id minting — all remain in the runner.
  No query/filter method in this version (deferred to #454 until the
  outcome-catalog consumer's PR lands with its known filter shape).

This mirrors the GoalBackend Protocol pattern (spec/41): coarse storage
primitives on the Protocol, computation in the runner layer.

See docs/spec/42-outcome-backend.md for the full normative contract.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .types import OutcomeCapabilities, OutcomeExport, OutcomeResult


@runtime_checkable
class OutcomeBackend(Protocol):
    """Contract every outcome backend implementation must satisfy.

    Implementations MUST NOT subclass this Protocol — it is structural.
    Implementations satisfy it by exposing the methods below with the
    documented behavior. The @runtime_checkable decorator enables
    isinstance(obj, OutcomeBackend) to perform a method-presence check
    (not a signature check — signatures are static-typing's job).

    Scope: bound at construction. FilesystemOutcomeBackend is constructed
    with agents_root + agent_name (or agent_root) and operates on
    <agent_root>/outcomes/runs/<run_id>/result.json.

    The backend is STATELESS at the Protocol level — it holds the agent_root
    path only. All in-memory state (the OutcomeResult envelope during the run)
    is managed by OutcomeRunner above the Protocol.

    write_result() is the write-once primitive — once a result.json is written
    for a run_id, no second write is permitted (spec/42 MUST 9). Implementations
    MUST enforce this by checking existence before writing.

    read_result() deserializes result.json back to an OutcomeResult with Path
    fields properly coerced (output_files as list[Path], artifact_path as
    Path | None). The from_dict() method on OutcomeResult handles this coercion.

    list_runs() enumerates run_ids from the outcomes/runs/ directory. MUST
    return [] when outcomes/runs/ does not exist (common for agents that have
    never run an outcome). MUST NOT raise FileNotFoundError.

    export() produces a portable OutcomeExport (spec/40 Exportable). Artifact
    refs in the export are relative-to-agent_root when possible (is_relative_to
    guard), falling back to absolute paths for artifacts outside agent_root.
    """

    @property
    def backend_id(self) -> str:
        """Stable identifier — e.g. 'filesystem', 'postgres'.

        Used by the registry for lookup and by diagnostic tooling. Treat as
        a backwards-compatibility surface — operator deployments may pin
        against these strings.
        """
        ...

    def write_result(self, agent_id: str, run_id: str, result: OutcomeResult) -> None:
        """Write result.json for a completed outcome run.

        Write-once contract (spec/42 MUST 9): once a result.json is written
        for a run_id, no second write is permitted. Raises AtomicAgentsError
        if result.json already exists for this run_id.

        Atomic: uses temp+fsync+rename so the file is always complete on disk.

        Args:
            agent_id: the agent directory name (relative to agents_root).
            run_id: the run identifier (directory name under outcomes/runs/).
            result: the OutcomeResult to write. Path fields are serialized as
                absolute path strings; the serialization is byte-identical to
                the runner's _write_result_json (TEST 30 pins this). Since #448
                PR2 the runner routes its write through this method, so a run
                launched with a custom --output-dir now lands result.json at the
                canonical outcomes/runs/<run_id>/ (the agent's artifact files
                still go to --output-dir) — see spec/42 §"Artifact-reference
                portability".

        Raises:
            AtomicAgentsError: when result.json already exists for this run_id
                (write-once contract violation).
        """
        ...

    def read_result(self, agent_id: str, run_id: str) -> OutcomeResult:
        """Read and reconstruct an OutcomeResult from result.json.

        Args:
            agent_id: the agent directory name.
            run_id: the run identifier.

        Returns:
            OutcomeResult with Path fields properly coerced (output_files
            as list[Path], IterationRecord.artifact_path as Path | None).

        Raises:
            AtomicAgentsError: when result.json is absent for this run_id.
            OutcomeCorrupted: when result.json is present but unparseable or
                missing required fields.
        """
        ...

    def list_runs(self, agent_id: str) -> list[str]:
        """Enumerate run_ids for all completed outcome runs.

        MUST return [] when outcomes/runs/ does not exist (common for agents
        that have never completed an outcome run). MUST NOT raise
        FileNotFoundError.

        Returns:
            List of run_id strings (directory names under outcomes/runs/).
            Sorted in lexicographic order (matching spec/36 MUST 5 alignment).
        """
        ...

    def export(self, query: Any = None) -> OutcomeExport:
        """Export the most recent completed outcome run as a canonical OutcomeExport.

        Enumerates via list_runs() (not semantic query). Best-effort
        point-in-time snapshot of the most recent run.

        Artifact refs in the export are relative-to-agent_root when the
        artifact was written under agent_root (the common case). Artifacts
        written to an output_dir outside agent_root are exported as absolute
        paths (the is_relative_to fallback). Callers requiring fully portable
        exports MUST ensure output_dir is under agent_root.

        UTF-8, no CRLF in JSON output (json.dumps produces LF-only).

        Args:
            query: unused (reserved for future bounded-export filtering, e.g.
                when supports_run_query ships).

        Returns:
            OutcomeExport for the most recent run, or an empty OutcomeExport
            (empty result_json_bytes, empty artifact_refs) when no runs exist.
        """
        ...

    def export_all(self) -> OutcomeExport:
        """Convenience alias for export(None).

        NOTE: despite the spec/40 ``Exportable`` meaning of ``export_all`` ("all
        records, no filter"), OutcomeBackend's ``OutcomeExport`` shape is
        single-run, so this returns ONLY the most-recent run (identical to
        ``export()``). See ``export()`` and spec/42 §"Export fidelity" for the
        documented divergence (follow-up #454).
        """
        ...

    def capabilities(self) -> OutcomeCapabilities:
        """Backend capability declaration — see OutcomeCapabilities.

        Conformance tests assert claim-vs-behavior parity. Honest capabilities
        let callers fail fast against incompatible backends.
        """
        ...

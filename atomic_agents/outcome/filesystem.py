"""FilesystemOutcomeBackend — directory-tree reference implementation (spec/42).

This is the default backend for single-host deployments. It wraps the same
on-disk shape OutcomeRunner has used since the framework's first outcome support:
    <agent_root>/outcomes/runs/<run_id>/result.json  — completed run result

The on-disk result.json is BYTE-IDENTICAL to what OutcomeRunner._write_result_json
produces today — zero behavior change for existing deployments. The write_result()
method lifts _write_result_json's asdict + Path-coercion + atomic_write logic
verbatim (artifact-reference-portability Option C ruling: on-disk stays absolute).

The export() method is NET-NEW and emits PORTABLE artifact references by rebasing
absolute artifact paths to relative-to-AGENT_ROOT using the is_relative_to guard
(Option C ruling). The on-disk result.json bytes are NOT changed by export().

Construction is side-effect-free (no filesystem I/O in __init__).

Write-once contract (spec/42 MUST 9):
    write_result() checks existence before writing. Once a result.json is written
    for a run_id, no second write is permitted. The check-then-write window is
    not a meaningful TOCTOU risk because run_ids are UUID-keyed (no collision in
    practice) and the only realistic race is a deliberate double-call.

Rebasing base — agent_root, NOT agents_root (deliberate departure from the
artifact-reference-portability ruling's literal `relative_to(self.agents_root)`
pattern):
    The arc ruling cited the `relative_to(self.agents_root)` pattern at the old
    outcome.py:555/565-566/572 sites as the rebasing shape. This impl rebases
    against agent_root (the single agent's own root) instead, because Principle #1
    ("same files = same agent") is about a WHOLE-AGENT move: an export whose
    artifact refs are relative to the agent's own root survives the agent dir being
    relocated anywhere on disk, which is the portability property OutcomeBackend
    exists to deliver (T15 / Position B). agents_root-relative (fleet-prefixed,
    `agentname/outcomes/...`) would bake the fleet layout into the export. The
    departure is recorded in spec/42 §"Artifact-reference portability (Option C)"
    and the PR body. agents_root is therefore NOT stored — the backend is scoped
    to one agent_root, derived as agents_root / agent_name at construction.

Import boundary (circular-import safety):
    - Imports only from ..exceptions, .._io, .types — no imports from
      ..outcome (the shim) or any module that imports ..outcome at module level.
      This keeps outcome/__init__.py importable without loading the LLM stack.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .._io import atomic_write, safe_resolve_under
from ..exceptions import AtomicAgentsError, OutcomeCorrupted, PathTraversalError
from .types import OutcomeCapabilities, OutcomeExport, OutcomeResult


class FilesystemOutcomeBackend:
    """Filesystem reference impl for OutcomeBackend Protocol (spec/42).

    Scoped to one agent root — <agent_root>/outcomes/runs/<run_id>/result.json.
    Constructed once per agent; construction is side-effect-free (no filesystem
    I/O in __init__).

    export() rebases artifact refs against agent_root (this agent's own root),
    NOT agents_root — see the module docstring for the rationale and the recorded
    departure from the arc ruling's literal pattern. agent_root is derived as
    agents_root / agent_name at construction; agents_root itself is not retained.

    Args:
        agents_root: the fleet-level root directory (parent of all agent dirs).
        agent_name: the agent directory name (relative to agents_root). The
            backend's agent_root is agents_root / agent_name.
    """

    def __init__(
        self,
        agents_root: Path,
        agent_name: str,
    ):
        self._agent_root = agents_root / agent_name

    @property
    def backend_id(self) -> str:
        """Stable backend identifier."""
        return "filesystem"

    def _runs_root(self) -> Path:
        """The resolved ``outcomes/runs`` root, verified to stay under agent_root.

        ``safe_resolve_under(run_id, runs_root)`` trusts ``runs_root`` as the
        containment anchor, so if ``outcomes`` or ``outcomes/runs`` is ITSELF a
        symlink to ``/outside``, every run_id would "contain" under the
        out-of-vault target and escape. Anchor the trust at agent_root: the
        resolved runs root MUST stay under ``agent_root``. Raises
        PathTraversalError (an AtomicAgentsError) on a symlinked-ancestor escape.
        """
        agent_root_resolved = self._agent_root.resolve()
        runs_root_resolved = (self._agent_root / "outcomes" / "runs").resolve()
        if not runs_root_resolved.is_relative_to(agent_root_resolved):
            raise PathTraversalError(
                "outcomes/runs resolves outside the agent vault "
                "(symlinked ancestor refused)",
                child="outcomes/runs",
                root=str(agent_root_resolved),
            )
        return runs_root_resolved

    def _run_dir(self, run_id: str) -> Path:
        """Resolve the run directory for a caller-supplied run_id, refusing escape.

        ``run_id`` is external input on the Protocol surface (the contract does
        NOT guarantee it is UUID-shaped). Three checks, all raising
        PathTraversalError (an AtomicAgentsError):

        0. The ``outcomes/runs`` root itself must stay under agent_root — a
           symlinked ancestor cannot become the trusted containment root
           (see _runs_root).
        1. safe_resolve_under refuses out-of-root escape (``../../other-agent``,
           absolute paths) — matching memory/filesystem.py and the framework
           invariant "Path traversal refused per _io.safe_resolve_under".
        2. The resolved dir MUST be a single direct child of ``runs/``. This
           refuses in-root degenerate ids that pass check 1 but break the
           one-directory-per-run invariant: ``""`` / ``"."`` (resolve to
           ``runs/`` itself, silently writing ``runs/result.json`` — invisible to
           list_runs and poisoning the write-once check) and nested ``"a/b"``
           (written but never enumerated).
        """
        runs_root_resolved = self._runs_root()
        resolved = safe_resolve_under(run_id, runs_root_resolved)
        if resolved.parent != runs_root_resolved:
            raise PathTraversalError(
                f"run_id {run_id!r} must be a single run-directory name "
                f"(not empty, '.', or nested)",
                child=str(run_id),
                root=str(runs_root_resolved),
            )
        return resolved

    def _validated_result_path(self, run_id: str) -> Path:
        """result.json path for run_id with run-dir AND file containment checked.

        ``_run_dir`` refuses a symlinked/escaping run directory (it resolves the
        run dir and verifies containment). This additionally refuses a symlinked
        result.json so a planted symlink cannot make read_result()/export() read
        an arbitrary host file outside the vault (the T15 containment boundary).
        Raises PathTraversalError (an AtomicAgentsError) on either escape.
        """
        result_path = self._run_dir(run_id) / "result.json"
        if result_path.is_symlink():
            raise PathTraversalError(
                f"result.json for run_id {run_id!r} is a symlink (refused)",
                child=str(run_id),
                root=str((self._agent_root / "outcomes" / "runs").resolve()),
            )
        return result_path

    def write_result(self, agent_id: str, run_id: str, result: OutcomeResult) -> None:
        """Write result.json for a completed outcome run (write-once).

        Lifts _write_result_json's asdict + Path-coercion + atomic_write logic
        VERBATIM for byte-identical on-disk output. The golden-file conformance
        test asserts bytes-equality between this method and the existing runner.

        Write-once contract (spec/42 MUST 9): raises AtomicAgentsError if
        result.json already exists for this run_id.

        Args:
            agent_id: the agent directory name. Accepted for Protocol-signature
                uniformity; the filesystem backend is agent-scoped at
                construction (self._agent_root) and does not use agent_id to
                resolve the path.
            run_id: the run identifier (directory name under outcomes/runs/).
            result: the OutcomeResult to serialize.
        """
        run_dir = self._run_dir(run_id)
        result_path = run_dir / "result.json"

        # Write-once contract (spec/42 MUST 9)
        if result_path.exists():
            raise AtomicAgentsError(
                f"result.json already written for run_id={run_id!r}; "
                f"OutcomeBackend write-once contract (spec/42 MUST 9) forbids overwrite"
            )

        run_dir.mkdir(parents=True, exist_ok=True)

        # Serialize — VERBATIM lift of _write_result_json logic
        # (outcome/_outcome_impl.py:721-731) for byte-identical on-disk output.
        # Do NOT use to_dict() here — asdict()
        # is the base for write_result() to preserve field declaration order exactly.
        data = asdict(result)
        # Convert Path objects to strings (asdict() does NOT do this automatically)
        data["output_files"] = [str(p) for p in result.output_files]
        for rec in data["iterations"]:
            if rec.get("artifact_path") is not None:
                rec["artifact_path"] = str(rec["artifact_path"])
        atomic_write(result_path, json.dumps(data, indent=2))

    def read_result(self, agent_id: str, run_id: str) -> OutcomeResult:
        """Read and reconstruct an OutcomeResult from result.json.

        Args:
            agent_id: the agent directory name.
            run_id: the run identifier.

        Returns:
            OutcomeResult with Path fields coerced (output_files as list[Path],
            IterationRecord.artifact_path as Path | None).

        Raises:
            AtomicAgentsError: when result.json is absent for this run_id.
            OutcomeCorrupted: when result.json is present but unparseable or
                missing required fields.
        """
        result_path = self._validated_result_path(run_id)

        if not result_path.exists():
            raise AtomicAgentsError(
                f"result.json not found for agent={agent_id!r} run_id={run_id!r}: "
                f"{result_path}"
            )

        try:
            raw = result_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as e:
            raise OutcomeCorrupted(
                f"result.json is present but cannot be parsed as a valid OutcomeResult "
                f"for agent={agent_id!r} run_id={run_id!r}: {e}"
            ) from e

        # Valid JSON of the wrong shape (a list/string/number/null) is still a
        # corrupt OutcomeResult — from_dict would AttributeError on .get(), which
        # the except below does not catch. Reject it as OutcomeCorrupted per the
        # spec/42 contract (present-but-unparseable).
        if not isinstance(data, dict):
            raise OutcomeCorrupted(
                f"result.json is present but is not a JSON object "
                f"(got {type(data).__name__}) for agent={agent_id!r} run_id={run_id!r}"
            )

        try:
            return OutcomeResult.from_dict(data)
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise OutcomeCorrupted(
                f"result.json is missing required fields or has invalid types for "
                f"agent={agent_id!r} run_id={run_id!r}: {e}"
            ) from e

    def list_runs(self, agent_id: str) -> list[str]:
        """Enumerate run_ids for all completed outcome runs.

        MUST return [] when outcomes/runs/ does not exist.
        MUST NOT raise FileNotFoundError.

        Returns:
            Sorted list of run_id strings (lexicographic order, matching
            spec/36 MUST 5 alignment and the list_archived() convention).
        """
        # Anchor containment at agent_root: a symlinked 'outcomes'/'runs'
        # ancestor escaping the vault yields no runs (honoring the no-raise
        # contract) rather than enumerating an out-of-vault directory.
        try:
            runs_dir = self._runs_root()
        except PathTraversalError:
            return []
        if not runs_dir.exists():
            return []
        # Refuse symlinked run dirs / result.json: a legitimate run is a real
        # directory holding a real result.json (written via atomic_write). A
        # planted symlink (run dir -> /outside, or result.json -> /outside/x)
        # would otherwise be enumerated and read by export()/doctor, escaping
        # agent_root — the containment boundary T15 depends on.
        return sorted(
            d.name
            for d in runs_dir.iterdir()
            if d.is_dir()
            and not d.is_symlink()
            and (d / "result.json").exists()
            and not (d / "result.json").is_symlink()
        )

    def export(self, query: Any = None) -> OutcomeExport:
        """Export the most recent completed outcome run as a portable OutcomeExport.

        DELIBERATELY SINGLE-RUN: this returns ONLY the most-recent run
        (``list_runs()[-1]``), NOT every run. The ``OutcomeExport`` shape is
        single-run (one ``run_id`` + one ``result_json_bytes`` + one
        ``artifact_refs``) for this scaffolding PR — see spec/42 §"Export
        fidelity" for why and the spec/40 addendum cross-reference. This means
        ``export()``/``export_all()`` here are scoped to the latest run, NOT the
        spec/40 ``Exportable.export_all()`` "all records, no filter" wording; a
        multi-run export shape (carrying list[run]) is filed as a follow-up
        (issue #454) for when an outcome-catalog consumer needs it.

        Artifact paths in the export are relative to agent_root when the
        artifact was written under agent_root (the common case). Artifacts
        written to an output_dir outside agent_root are exported as absolute
        paths (the is_relative_to fallback).

        Callers requiring fully portable exports MUST ensure output_dir is
        under agent_root.

        Args:
            query: unused (reserved for future bounded-export filtering).

        Returns:
            OutcomeExport for the most recent run, or an empty OutcomeExport
            when no runs exist.
        """
        run_ids = self.list_runs(str(self._agent_root.name))
        if not run_ids:
            return OutcomeExport(
                run_id="",
                result_json_bytes=b"",
                artifact_refs=[],
                backend_id=self.backend_id,
                scope=str(self._agent_root),
            )

        # Most recent run (lexicographic — run_ids start with 'outcome-YYYYMMDD-...')
        run_id = run_ids[-1]
        # Validate containment (run_ids come from list_runs, which already filters
        # symlinks, but route through the same guard for defense-in-depth + TOCTOU).
        result_path = self._validated_result_path(run_id)

        try:
            result_json_bytes = result_path.read_bytes()
        except OSError:
            return OutcomeExport(
                run_id=run_id,
                result_json_bytes=b"",
                artifact_refs=[],
                backend_id=self.backend_id,
                scope=str(self._agent_root),
            )

        # Rebase artifact paths to relative-to-agent_root for portability.
        # Absolute paths outside agent_root are kept absolute (fallback).
        artifact_refs: list[str] = []
        try:
            data = json.loads(result_json_bytes)
        except json.JSONDecodeError:
            data = {}
        # Valid JSON of the wrong shape (list/scalar) would AttributeError on the
        # data.get() calls below — degrade to empty refs (bytes preserved) instead
        # of crashing the whole agent's export on one malformed run.
        if not isinstance(data, dict):
            data = {}

        # Collect all artifact paths: output_files + per-iteration artifact_path.
        # Defensively coerce EVERY nested field — a hand-edited / partially-written
        # / foreign result.json may set output_files or iterations to a non-list,
        # or an iteration to a non-dict. The top-level isinstance guard above does
        # NOT protect nested shapes, so each access degrades to a safe default
        # rather than crashing the whole agent's export.
        raw_output_files = data.get("output_files", [])
        all_artifact_paths: list[str] = (
            list(raw_output_files) if isinstance(raw_output_files, list) else []
        )
        raw_iterations = data.get("iterations", [])
        if isinstance(raw_iterations, list):
            for rec in raw_iterations:
                if not isinstance(rec, dict):
                    continue
                ap = rec.get("artifact_path")
                if ap is not None:
                    all_artifact_paths.append(ap)

        seen: set[str] = set()
        # Resolve the root once for the comparison. We compare RESOLVED paths on
        # both sides so a symlinked or unresolved root (e.g. macOS /tmp ->
        # /private/tmp, or a backend constructed with an unresolved agents_root
        # while result.json holds resolved absolute artifact paths) still rebases
        # to a portable relative ref instead of silently falling back to an
        # absolute (non-portable) one. This is comparison-only: self._agent_root
        # stays unresolved everywhere else so the on-disk write path is unchanged
        # (the byte-identity / zero-behavior-change guard).
        root_resolved = self._agent_root.resolve()
        for ap_str in all_artifact_paths:
            # artifact_refs is typed list[str]; skip non-string / empty entries so
            # a malformed numeric/None path can't leak a non-str into the export.
            if not isinstance(ap_str, str) or not ap_str or ap_str in seen:
                continue
            seen.add(ap_str)
            try:
                p_resolved = Path(ap_str).resolve()
                if p_resolved.is_relative_to(root_resolved):
                    artifact_refs.append(str(p_resolved.relative_to(root_resolved)))
                else:
                    # Fallback: keep the original absolute path
                    artifact_refs.append(ap_str)
            except (ValueError, TypeError):
                artifact_refs.append(ap_str)

        return OutcomeExport(
            run_id=run_id,
            result_json_bytes=result_json_bytes,
            artifact_refs=artifact_refs,
            backend_id=self.backend_id,
            scope=str(self._agent_root),
        )

    def export_all(self) -> OutcomeExport:
        """Convenience alias for export(None).

        NOTE: despite the ``export_all`` name (inherited from the spec/40
        ``Exportable`` surface, where it means "all records, no filter"),
        OutcomeBackend's ``export_all()`` returns ONLY the most-recent run —
        identical to ``export()``. The single-run ``OutcomeExport`` shape cannot
        carry multiple runs in this scaffolding PR. See ``export()`` and spec/42
        §"Export fidelity" for the documented divergence and follow-up #454.
        """
        return self.export(None)

    def capabilities(self) -> OutcomeCapabilities:
        """Backend capability declaration.

        FilesystemOutcomeBackend advertises:
        - supports_canonical_export=True (spec/40 Exportable at definition time)
        - supports_artifact_storage=True (run dir holds artifact files per spec/14)
        """
        return OutcomeCapabilities(
            backend_id=self.backend_id,
            supports_canonical_export=True,
            supports_artifact_storage=True,
        )

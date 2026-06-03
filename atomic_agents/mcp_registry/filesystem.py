"""FilesystemMCPServerRegistryBackend -- mcp.md-backed reference implementation.

Wraps the existing ``parse_mcp_md_text()`` semantics from ``atomic_agents/mcp.py``
behind the ``MCPServerRegistryBackend`` Protocol contract (spec/36). Each agent
has its own ``<agent_root>/mcp.md`` file; the backend is scoped per-agent via
``agent_root``.

Read paths only at PR 1. Install and uninstall ship in PR 3 alongside the
``LockBackend`` lease integration (per spec/36 D5 + D6 PR cadence).

Constructor signature stability: ``lock_backend`` is accepted but unused at PR 1
so PR 3 callers can pass the kwarg without breaking PR 1 callers. This matches
the spec/36 §"FilesystemMCPServerRegistryBackend" constructor note.
"""

from __future__ import annotations

import errno
import logging
import os
import re
import shutil
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from ..mcp import (
    MCPServerSpec,
    _resolve_env_vars,
    parse_mcp_md_text,
    validate_mcp_server_args,
)
from .backend import (
    MCPRegistryDescriptorInvalid,
    MCPRegistryUnavailable,
    MCPServerNotInRegistry,
)
from .types import MCPServerRef, MCPServerRegistryCapabilities, ValidationResult

if TYPE_CHECKING:
    from ..locks.backend import LockBackend

_logger = logging.getLogger(__name__)

# Charset rule from MUST 1. Matches CorpusBackend, PersonaBackend, PolicyBackend.
# Path-traversal tokens (``..``, ``/``, ``\\``), control chars, newlines,
# leading dots, and empty strings all fail this check.
_NAME_RE = re.compile(r"^[a-zA-Z0-9_.+@-]+$")

# Additional explicit refusal of path-traversal tokens that might still
# technically match the charset (e.g., a name that is purely dots).
_PATH_TRAVERSAL_TOKENS = frozenset({"..", "."})


def _validate_server_name(name: str) -> None:
    """Raise ``ValueError`` when ``name`` fails the MUST 1 charset rule.

    Checks at the API boundary BEFORE any backend access (spec/36 MUST 1).
    Validation logic mirrors ``CorpusBackend`` and ``PersonaBackend`` per
    prep-pass finding C-F2.

    Rejects: empty string, leading dot, path-traversal tokens (.., /, \\),
    control characters, newlines, and any character outside [a-zA-Z0-9_.+@-].
    """
    if not name:
        raise ValueError("MCP server name must not be empty.")
    if name.startswith("."):
        raise ValueError(
            f"MCP server name {name!r} must not start with '.'; "
            f"leading-dot names are reserved for hidden files."
        )
    if not _NAME_RE.match(name):
        raise ValueError(
            f"MCP server name {name!r} contains invalid characters. "
            f"Allowed: [a-zA-Z0-9_.+@-]"
        )
    if name in _PATH_TRAVERSAL_TOKENS:
        raise ValueError(
            f"MCP server name {name!r} is a path-traversal token and is not allowed."
        )


class FilesystemMCPServerRegistryBackend:
    """``mcp.md``-backed implementation of ``MCPServerRegistryBackend`` (spec/36).

    Reads ``<agent_root>/mcp.md`` for server declarations. Covers the read
    path only at PR 1 (list, load, load_all, validate, capabilities,
    refresh_capabilities, close). Install and uninstall land in PR 3 with the
    ``LockBackend`` lease integration.

    Constructor:

    ``agent_root``: the agent's directory. ``mcp.md`` lives at
        ``<agent_root>/mcp.md``. MAY not exist at construction -- MUST 2
        (side-effect-free construction): no file is opened here.

    ``read_paths``: list of ``Path`` objects the agent declares it may read
        (from ``tools.md``). Used by ``load_mcp_server(name)`` to apply the
        path-traversal check (Decision 8 of spec/36). Captured at construction;
        NOT applied at ``list_mcp_servers()`` time (list is metadata-only).

    ``lock_backend``: reserved for PR 3 (install/uninstall atomicity via
        ``LockBackend.acquire("mcp_registry", timeout=30)``). Accepted but
        unused at PR 1 so PR 3 callers can pass the kwarg without a constructor
        change. Operators passing a custom ``lock_backend`` MUST scope it to a
        registry-specific resource (e.g., ``.mcp_registry.lock``), NOT the
        agent's main ``.lock`` file.
    """

    def __init__(
        self,
        agent_root: Path,
        read_paths: list,
        *,
        lock_backend: LockBackend | None = None,
    ) -> None:
        # MUST 2: side-effect-free construction. No file opens, no subprocess
        # spawns, no network calls here.
        self._agent_root = agent_root
        self._read_paths = read_paths
        self._lock_backend = lock_backend  # unused at PR 1; reserved for PR 3

    # ─── Capability advertisement ─────────────────────────────────────────

    @property
    def capabilities(self) -> MCPServerRegistryCapabilities:
        """Static capability advertisement. Constant for the lifetime of this instance.

        ``supports_install`` and ``supports_uninstall`` are False at PR 1
        (methods ship in PR 3). Capability honesty (MUST 3): the conformance
        suite asserts that False-reported capabilities raise ``NotImplementedError``.
        This flips to True at PR 3 when the methods land.
        """
        return MCPServerRegistryCapabilities(
            supports_install=False,
            supports_uninstall=False,
            supports_capability_handshake=False,
            supports_audit=False,
            durable=True,
        )

    # ─── Backend identity ─────────────────────────────────────────────────

    @property
    def backend_id(self) -> str:
        """Stable identifier matching the registry key (MUST 6a)."""
        return "filesystem"

    # ─── Core discovery ───────────────────────────────────────────────────

    def list_mcp_servers(self) -> list[MCPServerRef]:
        """Return lightweight server refs from ``<agent_root>/mcp.md``.

        Returns ``[]`` when ``mcp.md`` is absent or empty (MUST 7 -- absent
        file is not an error). Lexicographic order by ``name`` (spec/36 §list
        semantics).

        CRITICAL: calls ``parse_mcp_md_text(content, resolve_env=False,
        read_paths=None)``. The ``read_paths=None`` is intentional per
        prep-pass Theme 3: passing ``self._read_paths`` here would trigger
        path-traversal validation at list time, violating Decision 8's
        "validation at load_mcp_server boundary" invariant. The
        ``resolve_env=False`` keeps ``$VAR`` references raw (they will be
        resolved at ``load_mcp_server`` time per MUST 8 / Decision 7).
        """
        mcp_md = self._agent_root / "mcp.md"
        if not mcp_md.exists():
            return []

        try:
            content = mcp_md.read_text(encoding="utf-8")
        except FileNotFoundError:
            # ENOENT: race between exists() check above and read; treat as absent.
            return []
        except OSError as exc:
            # Non-ENOENT (PermissionError, IsADirectoryError, etc.): transient
            # configuration error. Mirror load_mcp_server's MCPRegistryUnavailable
            # path (filesystem.py lines for load_mcp_server) for symmetry and to
            # surface the failure to PR 2's fail-closed wiring in agent.py:__init__.
            raise MCPRegistryUnavailable(f"cannot read {mcp_md}: {exc}") from exc

        try:
            specs = parse_mcp_md_text(
                content,
                mcp_md_path=mcp_md,
                read_paths=None,
                resolve_env=False,
            )
        except Exception as exc:
            _logger.warning(
                "FilesystemMCPServerRegistryBackend: mcp.md parse error: %s",
                exc,
            )
            return []

        refs = []
        for spec in specs:
            # P2 #1: Validate section names; skip and warn on invalid names to
            # prevent load_all_mcp_servers from raising uncaught ValueError on
            # a tampered or manually-edited mcp.md.
            try:
                _validate_server_name(spec.name)
            except ValueError:
                _logger.warning(
                    "FilesystemMCPServerRegistryBackend: skipping malformed section "
                    "name %r in mcp.md (failed charset validation)",
                    spec.name,
                )
                continue
            refs.append(
                MCPServerRef(
                    name=spec.name,
                    description=spec.description,
                    transport=spec.transport,
                    version=None,
                    source=f"mcp.md#section:{spec.name}",
                )
            )
        return sorted(refs, key=lambda r: r.name)

    def load_mcp_server(self, name: str) -> MCPServerSpec:
        """Return the fully-populated ``MCPServerSpec`` for the named server.

        Validates ``name`` charset at the API boundary BEFORE any disk access
        (MUST 1). Raises ``MCPServerNotInRegistry`` when the name is absent.
        Resolves ``$VAR`` env-var references at call time (MUST 8 / Decision 7).
        Applies path-traversal validation via ``validate_mcp_server_args``
        AFTER materialization (Decision 8 -- validation at this boundary).

        Implementation steps:
        1. Validate name charset -- raise ``ValueError`` on invalid.
        2. Re-parse ``mcp.md`` with ``resolve_env=False`` to get raw specs.
        3. Find the spec matching ``name`` -- raise ``MCPServerNotInRegistry``
           if absent.
        4. Resolve ``$VAR`` refs via ``_resolve_env_vars`` -- raises
           ``MCPServerConnectFailed`` on unresolvable refs (spec/19 exception).
        5. Apply path-traversal validation via ``validate_mcp_server_args``.
        6. Return the materialized spec.
        """
        _validate_server_name(name)

        mcp_md = self._agent_root / "mcp.md"
        if not mcp_md.exists():
            raise MCPServerNotInRegistry(
                f"MCP server {name!r} not found: mcp.md does not exist at {mcp_md}."
            )

        try:
            content = mcp_md.read_text(encoding="utf-8")
        except OSError as exc:
            # MUST 7: distinguish permanent absence (FileNotFoundError / ENOENT)
            # from transient failures (PermissionError, IsADirectoryError, etc.).
            # FileNotFoundError means the file does not exist -- permanent.
            # All other OSError subclasses indicate a transient configuration
            # problem (wrong permissions, EISDIR) -- raise MCPRegistryUnavailable
            # so callers know to retry rather than treating this as a catalog miss.
            if isinstance(exc, FileNotFoundError) or exc.errno == errno.ENOENT:
                raise MCPServerNotInRegistry(
                    f"MCP server {name!r} not found: mcp.md does not exist at {mcp_md}."
                ) from exc
            raise MCPRegistryUnavailable(
                f"MCP server {name!r} cannot be loaded: cannot read {mcp_md}: {exc}"
            ) from exc

        try:
            specs = parse_mcp_md_text(
                content,
                mcp_md_path=mcp_md,
                read_paths=None,
                resolve_env=False,
            )
        except Exception as exc:
            raise MCPRegistryDescriptorInvalid(
                f"mcp.md at {mcp_md} could not be parsed: {exc}"
            ) from exc

        # P1 #2: Detect malformed sections (H2 header exists but parse failed).
        # If the name appears in an H2 header but not in the parsed spec list,
        # the section is malformed (e.g., missing command:). Raise a more
        # informative MCPRegistryDescriptorInvalid instead of MCPServerNotInRegistry.
        h2_names = set(re.findall(r"^## (\S+)", content, re.MULTILINE))

        # Find the matching spec by name.
        matched: MCPServerSpec | None = None
        for spec in specs:
            if spec.name == name:
                matched = spec
                break

        if matched is None:
            if name in h2_names:
                raise MCPRegistryDescriptorInvalid(
                    f"MCP server {name!r} has a malformed descriptor in mcp.md "
                    f"(section exists but parse failed; check that 'command:' is present)"
                )
            raise MCPServerNotInRegistry(
                f"MCP server {name!r} not found in {mcp_md}. "
                f"Available: {sorted(s.name for s in specs)}"
            )

        # Resolve $VAR env refs at load time (MUST 8 / Decision 7).
        # _resolve_env_vars raises MCPServerConnectFailed on unresolvable refs,
        # matching the spec/19 error shape exactly.
        resolved_env = _resolve_env_vars(matched.env, name)

        # Reconstruct the spec with resolved env. MCPServerSpec is a plain
        # dataclass (not frozen), so we create a fresh instance.
        materialized = replace(matched, env=resolved_env)

        # Apply path-traversal check AFTER materialization (Decision 8).
        if self._read_paths:
            validate_mcp_server_args(materialized, self._read_paths)

        return materialized

    def load_all_mcp_servers(self) -> list[MCPServerSpec]:
        """Return all mounted ``MCPServerSpec`` instances in bulk.

        Reads mcp.md once and parses, then resolves env vars per spec
        (Decision 7). Distinct from the default ``_default_load_all`` (which
        iterates ``list_mcp_servers`` then calls ``load_mcp_server`` per ref)
        because that pattern masks transient and parse failures:
        ``list_mcp_servers`` used to catch all OSError before the PR 2 fix.
        The single read-parse here maps:
          - ENOENT: empty list (correct -- "no mcp.md is not a probe failure")
          - Other OSError: MCPRegistryUnavailable (transient)
          - Parse error: MCPRegistryDescriptorInvalid (permanent descriptor problem)
          - Env-var unresolvable: MCPServerConnectFailed per spec/19

        PR 2 framework-level fail-closed semantic (spec/36) depends on these
        distinct exceptions surfacing correctly.
        """
        mcp_md = self._agent_root / "mcp.md"
        if not mcp_md.exists():
            return []

        try:
            content = mcp_md.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise MCPRegistryUnavailable(f"cannot read {mcp_md}: {exc}") from exc

        try:
            specs = parse_mcp_md_text(
                content,
                mcp_md_path=mcp_md,
                read_paths=None,
                resolve_env=False,
            )
        except Exception as exc:
            raise MCPRegistryDescriptorInvalid(
                f"mcp.md at {mcp_md} could not be parsed: {exc}"
            ) from exc

        materialized = []
        for spec in specs:
            try:
                _validate_server_name(spec.name)
            except ValueError:
                _logger.warning(
                    "FilesystemMCPServerRegistryBackend: skipping malformed "
                    "section name %r in mcp.md (failed charset validation)",
                    spec.name,
                )
                continue
            resolved_env = _resolve_env_vars(spec.env, spec.name)
            materialized_spec = replace(spec, env=resolved_env)
            if self._read_paths:
                validate_mcp_server_args(materialized_spec, self._read_paths)
            materialized.append(materialized_spec)

        # Sort lexicographically per MUST 5 (consistent with list_mcp_servers).
        materialized.sort(key=lambda s: s.name)
        return materialized

    def validate(self, name: str) -> ValidationResult:
        """Static check of the named server descriptor.

        Does NOT spawn the MCP subprocess (MUST 2 analog -- static only).
        Returns ``ValidationResult(ok=False, errors=[...])`` when the server
        is absent; does NOT raise ``MCPServerNotInRegistry``.

        Checks:
        - Descriptor parses from mcp.md.
        - ``command`` exists on PATH (warn if absent -- do not fail; PATH at
          run time may differ).
        - ``transport`` value is recognized (``"stdio"`` is the only supported
          value in v1).
        - ``$VAR`` refs resolve against current ``os.environ`` (warn if not --
          do not fail; env may be set at agent run time).

        Validates ``name`` charset at the API boundary (MUST 1).
        """
        try:
            _validate_server_name(name)
        except ValueError as exc:
            return ValidationResult(ok=False, errors=[str(exc)], warnings=[])

        mcp_md = self._agent_root / "mcp.md"
        if not mcp_md.exists():
            return ValidationResult(
                ok=False,
                errors=[
                    f"MCP server {name!r} not found: mcp.md does not exist at {mcp_md}."
                ],
                warnings=[],
            )

        try:
            content = mcp_md.read_text(encoding="utf-8")
            specs = parse_mcp_md_text(
                content,
                mcp_md_path=mcp_md,
                read_paths=None,
                resolve_env=False,
            )
        except Exception as exc:
            return ValidationResult(
                ok=False,
                errors=[f"mcp.md parse error: {exc}"],
                warnings=[],
            )

        matched: MCPServerSpec | None = None
        for spec in specs:
            if spec.name == name:
                matched = spec
                break

        if matched is None:
            available = sorted(s.name for s in specs)
            return ValidationResult(
                ok=False,
                errors=[f"MCP server {name!r} not in registry. Available: {available}"],
                warnings=[],
            )

        errors: list[str] = []
        warnings: list[str] = []

        # Check command exists on PATH (warn, not error -- PATH may differ at runtime).
        if matched.command and shutil.which(matched.command) is None:
            warnings.append(
                f"command {matched.command!r} not found on PATH; "
                f"may be available at agent run time."
            )

        # Check transport is recognized.
        if matched.transport not in ("stdio",):
            errors.append(
                f"transport {matched.transport!r} is not recognized. "
                f"Only 'stdio' is supported in v1."
            )

        # Check $VAR refs resolve (warn, not error -- env may be set at run time).
        for env_key, env_val in matched.env.items():
            if env_val.startswith("$"):
                var_name = env_val[1:]
                if os.environ.get(var_name) is None:
                    warnings.append(
                        f"env var ${var_name} (for {env_key!r}) not set in current "
                        f"process; must be set at agent run time."
                    )

        ok = len(errors) == 0
        return ValidationResult(ok=ok, errors=errors, warnings=warnings)

    # ─── Capability-gated lifecycle (PR 3) ───────────────────────────────

    def install(self, spec: MCPServerSpec) -> MCPServerRef:
        """Not implemented at PR 1. Lands in PR 3 with LockBackend integration.

        ``capabilities.supports_install=False`` at this PR -- conformance suite
        asserts this path raises ``NotImplementedError`` when ``supports_install``
        reports False (MUST 3).
        """
        raise NotImplementedError(
            "FilesystemMCPServerRegistryBackend.install lands in PR 3 alongside "
            "LockBackend integration and atomic mcp.md write semantics."
        )

    def uninstall(self, name: str) -> None:
        """Not implemented at PR 1. Lands in PR 3 with LockBackend integration.

        ``capabilities.supports_uninstall=False`` at this PR -- conformance suite
        asserts this path raises ``NotImplementedError`` when ``supports_uninstall``
        reports False (MUST 3).
        """
        raise NotImplementedError(
            "FilesystemMCPServerRegistryBackend.uninstall lands in PR 3 alongside "
            "LockBackend integration and atomic mcp.md write semantics."
        )

    # ─── Lifecycle ────────────────────────────────────────────────────────

    def refresh_capabilities(self) -> MCPServerRegistryCapabilities:
        """Filesystem capabilities are static -- returns the cached constant.

        No-op refresh because filesystem has no remote dependency. The
        ``capabilities`` property returns the same frozen instance every time.
        """
        return self.capabilities

    def close(self) -> None:
        """No-op. Filesystem backend holds no open resources.

        Idempotent (MUST 6b): calling ``close()`` twice does not raise.
        """
